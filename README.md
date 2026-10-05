# DocForensics Analyzer — Phase 0/1 Scaffold

This is a starting skeleton, not the finished app. Get every piece below
working on your own machine before handing anything else to Antigravity.

## 1. Setup

```
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

Runtime dependencies are just PySide6 (GUI), PyMuPDF (rendering) and
pdfplumber (word extraction) — no system binaries needed.

## 2. Verify the core pipeline first

```
python dev_tests/test_extraction_diff_smoke.py
QT_QPA_PLATFORM=offscreen python dev_tests/manual_e2e_compare_check.py
```

All tests should print `PASS`. The first drives extraction + diffing
headlessly (no GUI); the second drives the real Qt widgets offscreen.

## 3. Run the MVP-0 GUI

```
python main.py
```

Click "Load PDF", pick any PDF, and confirm page 1 renders. That's the
entire scope of this milestone — no second file, no diffing, no zoom.

## 4. Folder structure

```
core/      — extraction, normalization, diffing logic (NO Qt imports)
           • pdf_extractor.py  — word stream + coordinates (pdfplumber)
           • diff_engine.py    — word-level diff, severity, per-section diff
           • section_diff.py   — section-level pipeline: column reading
                                 order, header maps, keyed diff (fitz +
                                 diff-match-patch)
ui/        — PySide6 windows/widgets (calls into core/ and workers/)
workers/   — QRunnable wrappers around core/ logic for QThreadPool
dev_tests/ — standalone smoke tests, run without launching the GUI
```

## 5. MVP-3 (in progress): word-level extraction + diffing

`core/pdf_extractor.py` and `core/diff_engine.py` — no Qt, no GUI wiring
yet. This slice only proves the extraction+comparison logic works on
its own:

```
python dev_tests/test_extraction_diff_smoke.py
```

Every test (1-10) should print `PASS`. Design notes are in the
module docstrings — short version: we diff a flat per-page WORD stream
(via `pdfplumber.extract_words`), not raw lines/blocks, specifically so
that reflow (line wraps + uneven gaps caused by resizing a PDF) never
produces a false diff. Each word keeps its bounding box the whole way
through, so a future UI can jump straight to the word on click without
re-extracting anything.

Not done yet, on purpose: multi-column reading order, a real severity
ruleset, and any UI wiring (`workers/diff_worker.py` to run this off
the GUI thread, and a diff-results panel with click-to-navigate). Each
of those is its own future slice.

## 6. MVP-4 (current): Compare button, results table, click-to-navigate

`workers/diff_worker.py` runs MVP-3's pipeline off the GUI thread;
`ui/results_panel.py` lists the results, color-coded by severity;
`main.py` adds the "Compare" button (enabled once both PDFs are
loaded) and wires clicking a result to `DocumentPanel.highlight_pdf_bbox()`,
which highlights the word and scrolls it into view on whichever
panel(s) it appears on.

Run it:

```
python main.py
```

Load an Original and a Suspect PDF, click "Compare", then click any
row in the results table at the bottom — it highlights and scrolls to
that word on the corresponding panel(s).

There's also a headless check (`dev_tests/manual_e2e_compare_check.py`)
that drives the whole flow — load, compare, click a result — without a
display, using `QT_QPA_PLATFORM=offscreen`:

```
QT_QPA_PLATFORM=offscreen python dev_tests/manual_e2e_compare_check.py
```

**Known limitation, unchanged from MVP-2:** panels only ever render
page 1. If a diff is on page 2+, clicking it shows a status-bar message
saying navigation isn't available there yet, rather than silently
failing or highlighting the wrong spot. Multi-page rendering is its
own future slice (fixed in MVP-6, below).

## 7. MVP-5 (current): column-aware reading order + severity ruleset

Two real problems showed up once this was run against real, differently-
laid-out sample PDFs (a 1-column "approved" reference vs. a 3-column
"suspect" copy of the same drug label):

**Column reading order.** `core/pdf_extractor.py` used to sort every
word on a page by vertical position only, so on the 3-column suspect
PDF, words from all three columns that happened to sit at the same
height got interleaved into one nonsense "line". `extract_words()`
now detects column bands per page (any number of columns, including
the common case of 1) and orders words column-by-column, top-to-bottom
within each column, before moving to the next column. See the module
docstring for how gutter detection works and its known limitation
(a wide table's internal cell gutters can still confuse page-level
column detection -- isolating table regions first is a good candidate
for a future slice).

**Severity ruleset.** Real source PDFs carry pre-existing text-layer
typos that have nothing to do with tampering (e.g. "especially" ->
"especial y", "illness" -> "il ness" -- a dropped double letter plus a
stray space). `core/diff_engine.py` now runs a strict priority ladder:
any digit change (dosage/strength/quantity) is unconditionally
"critical" and is never eligible for the downgrade below; then
case-only -> "major"; punctuation-only -> "minor"; then a new lowest
tier, "typo", for alphabetic-only changes that are highly similar once
whitespace is stripped; everything else genuine stays "critical".
Results are sorted critical-first. `ui/results_panel.py` colors the
new tier and adds a "Hide typo-level differences" checkbox (off by
default -- nothing is hidden unless you ask) so a reviewer can
declutter the table while triaging without anything being silently
dropped from the underlying results.

Run the updated checks:

```
python dev_tests/test_extraction_diff_smoke.py
```

TEST 1-2 are unchanged from MVP-3. TEST 3 builds a synthetic 3-column
PDF and checks it diffs as zero changes against the same wording laid
out in one column. TEST 4 checks the priority ladder: a dosage digit
change must come back "critical", a dropped-letter/stray-space typo
must come back "typo".

## 8. MVP-6 (current): multi-page navigation + results-table readability

Two more real problems, found by actually using MVP-5 end to end:

**Multi-page navigation.** `ui/document_panel.py` used to render only
page 1. `render_pdf()` (renamed from `render_first_page()`) now renders
every page into one continuous, vertically-stacked scene -- like a
normal PDF viewer's scroll view -- with a small gap between pages, and
adds Prev/Next buttons + a "Page X / N" label so a reviewer can page
through independently of any diff click. `highlight_pdf_bbox(page,
bbox)` now works for any page: it just offsets the bbox by that page's
Y position in the stacked scene. It only returns `False` (and
`main.py` only shows a status message) when a page genuinely doesn't
exist in that particular document -- e.g. the Approved PDF has 4 pages
and the Suspect PDF has 2, and a diff points at page 3 or 4, which
only exists on the Approved side.

**Results-table readability.** The MVP-4 table tinted an entire row
(severity + type + both text columns) with a pale pastel color and
left the text color to whatever the app/OS theme defaulted to --
exactly the combination that makes text hard to read, especially
under a dark theme. `ui/results_panel.py` now:
- Puts severity color ONLY on a single high-contrast "badge" cell
  (dark/saturated background, explicit bold white or near-black text
  chosen per tier for contrast) instead of tinting the whole row.
- Gives the Type/Approved/Suspect columns a fixed, theme-independent
  palette: alternating near-white/light-gray backgrounds with explicit
  dark text, a styled dark header, and visible gridlines -- readable
  regardless of the system's light/dark theme.
- Truncates very long passages (e.g. a large deleted block) with an
  ellipsis and always keeps the full text available via tooltip and
  auto-sized row height, so nothing is hidden and no row balloons out
  of control.

Run it:

```
python main.py
```

Load an Original and a Suspect PDF (either can now have any number of
pages), click "Compare", and use the results table's colored severity
badges plus the Prev/Next page controls on each panel to review every
discrepancy, on any page.

## 9. MVP-7 (current): table-aware reading order + Unicode normalization

Running MVP-6 against real medical/regulatory layouts surfaced the
**"double- Dizziness 11 5 When" -> "doubleWhen"** failure pattern: a
wide ruled table's internal cell gutters were being mistaken for page
column gutters, so `core/pdf_extractor.py` sliced straight through the
table and read it column-major (scrambling cells like "Dizziness 11 5
When"), and then the line-end hyphen in one cell was stitched onto the
first word of the *next* cell ("double-" + "When" -> "doubleWhen"), a
phantom token that matched nothing on the other side. When the two
PDFs' column geometry differed even slightly, the scrambles diverged
and the diff filled with false "added/removed" pairs.

MVP-7 fixes extraction so this can never happen:

- **Tables are detected FIRST** via pdfplumber's `find_tables()` on
  ruling lines (`core/pdf_extractor.py::_detect_page_tables`). Every
  word inside a table is assigned to its grid cell (row/col) and
  ordered **cell-by-cell, row-major** — rows top-to-bottom, cells
  left-to-right within a row, words in reading order within a cell
  (`_order_page_words` / `_table_units_for`). The MVP-5 column-band
  heuristic now runs on **body words only**, so table gutters can no
  longer confuse it, and table rows are interleaved back into their
  band by vertical position.
- **Hyphen-stitching can never cross a cell boundary.**
  `Word` carries a `cell_id`; `build_comparison_tokens()` only stitches
  a line-end hyphen onto the next word when both are in the same cell
  (or both are body text). A hyphen at the end of one cell is left
  alone instead of being merged with the first word of the next row.
- **Every word is Unicode-normalized once at extraction time**
  (`_normalize_word_text`): NFC, zero-width chars, soft hyphens,
  non-breaking spaces (so "5\u00a0mg" and "5 mg" compare equal), and
  common ligatures (ﬁ/ﬂ/ﬀ/ﬃ/ﬄ/ﬅ/ﬆ). Both documents pass through the
  exact same pipeline before comparison.

Validated against the real pair that produced the reported errors
(the 2-page ROPINIROLE label "Approved Artwork.pdf" vs "Artwork with
correction.pdf"), which exposed two more failure modes, both fixed:

- **Whole-page "tables".** The Highlights box outline makes
  pdfplumber detect ONE table covering 83% of the page; claiming every
  word into a single cell flattened the page to a raw (top, x0) sort
  (the MVP-4 bug in disguise). `_detect_page_tables` now rejects any
  table covering more than 50% of the page as a layout frame, and the
  real adverse-reaction tables beneath it are read row-major again.
- **Gutter off-by-one.** The column-gutter sweep measured the truly
  empty runs between columns at exactly 11 points (line-endings at the
  column edges cover the boundary points) and the `>= min_gutter`
  check rejected every real gutter on the 6-column page by one point,
  silently falling back to y-major ordering. The width test is now
  inclusive (`end - start + 1`), so column-major reading order engages
  and wrap-point jitter between the two renderings can no longer
  reorder the stream.
- **"Added/Removed invert pairs".** The few artifacts that survive
  even with correct ordering are identical text present once on each
  side at a different stream position (e.g. `stearate,` that wrapped
  to an adjacent line). `core/diff_engine.py` now runs a pairing pass:
  `X removed` + `X added` with the same text are relabeled
  "reordered" at a new lowest severity tier (sorted last, shown in
  the results table with its own badge, never dropped).

Run the updated check (now 7 tests):

```
python dev_tests/test_extraction_diff_smoke.py
```

TEST 5 builds the reported table pattern — identical content, two
radically different geometries — and asserts the table is read
row-major, that no "doubleWhen"-style phantom token exists, that
"double-" survives standalone, and that the two renderings diff as
ZERO. TEST 6 unit-checks the normalization and the stitch rules
(body text still stitches; cells never do). TEST 7 checks the
reorder-pairing pass: identical text in a different stream position
comes back as the lowest "reorder" tier, while a genuine wording
change stays critical.

**Trackpad zoom (same slice):** the document viewer also gained
pinch-to-zoom. A two-finger pinch on either panel (`QPinchGesture` on
Windows precision touchpads / macOS trackpads) — plus the Ctrl+wheel
shortcut many touchpads emit for a pinch — zooms BOTH panels through
the same shared factor as the toolbar +/- buttons, so you never have
to click them. Plain wheel scrolling is untouched.

**Missing-text navigation (same slice):** an inserted/deleted word
only exists on ONE side, so clicking its result used to navigate just
that one panel. Now the other panel draws a dashed red "missing"
marker at the word's **before coordinates** (the position it occupies
on the side that still has it), so a removed/added item navigates
BOTH documents. Each panel centers on its own marker; the scroll
mirror is suppressed during navigation so the second panel's move
can't pull the first one off its highlight (important when the two
PDFs' pages differ in size, which puts the same bbox at different
scroll positions). If the missing text sits on a page the other
document doesn't have at all (e.g. original page 3 vs a 2-page
suspect), the panel still navigates — the marker clamps to a band
across the bottom of its last page and the status bar says which
page of the other document the text is on.

**Per-line highlights (same slice):** the diff engine now reports
EACH discrepancy as a list of per-line boxes (`Location.boxes`),
not one merged rectangle. A removed paragraph spanning ten lines
used to be highlighted as one tall vertical box slicing through
unrelated content; now every visual line gets its own horizontal
highlight (and the missing-text marker does the same), so the
highlight hugs the actual text. The union box is kept only as the
navigation/clamp target.

**Ignore minor toggle (same slice):** the results table now has an
"Ignore minor and below" checkbox next to the existing typo one. It
hides punctuation-only, typo-level, and reorder rows so a reviewer
can focus on critical + major findings; like the typo checkbox it
only filters the table view — the underlying results are never
dropped.

**Out-of-order section matching (MVP-8):** when BOTH documents
contain at least two numbered section headers ("1 INDICATIONS AND
USAGE"), the comparison no longer runs as one linear word stream.
`diff_documents` chunks each document at the headers — including
wrapped headers (an A4 leaflet renders "1 INDICATIONS AND" / "USAGE"
on two lines) and numbered SUBSECTIONS ("2.1 Dosage in adults",
"5.3 Abnormal Thinking") — then matches every suspect section
against ALL approved sections, position-agnostic. Matching is
TITLE-ANCHORED first (same header number + similar title pairs "2
DOSAGE" with "2 DOSAGE" even when a column-interleaved layout made
their bodies look alike), with token-sequence similarity as the
fallback, and each matched pair is diffed in isolation. A section
whose matched position changed is reported as ONE low-severity
"Reordered Section" row instead of a critical removed/added pair (or
worse, a cascade of mismatch noise across everything between the two
positions); a genuine edit inside a moved section is still caught at
its real severity, with its real coordinates. Sections present on
only one side become a single removed/added row — never a cascade.
Finally, multi-word runs that exist VERBATIM elsewhere in the other
document (a section fragment printed in a different column of a
leaflet — the content is on both sides, just relocated by layout)
are demoted to "reorder" instead of reporting false critical
removals/additions; runs that differ by even one word stay at their
real severity. Documents without detected numbered headers (plain
paragraphs, tables) fall back to the original whole-stream word
diff, unchanged. Header recognition is deliberately conservative: a
numbered line whose remaining words are all caps ("1 INDICATIONS AND
USAGE"), or a lone number line above an all-caps line; header lines
inside ruled tables are ignored.

**Known limitation, unchanged for now:** only tables with visible
ruling lines are auto-detected. A pure-whitespace "borderless" table
is deliberately NOT detected — pdfplumber's word-alignment fallback
was tried and rejected, because a borderless grid is geometrically
indistinguishable from a legitimate multi-column body layout (aligned
left edges, regular gutters), so it misdetects justified paragraphs
and real 3-column labels as tables, reintroducing the exact false
positives this milestone removes.

## 10. Next milestone (MVP-8)

Good candidates for the next narrow slice, once MVP-7 runs cleanly
against your real sample PDFs:

> "Add a page-thumbnail strip or jump-to-page spinbox to
> DocumentPanel, and/or a small severity-count summary bar above the
> results table (e.g. '4 critical · 5 minor · 14 typo')." — a
> lightweight orientation aid once documents get longer.

> "Borderless-table detection: only treat a word-aligned grid as a
> table when the page has NO plausible column-band layout for the
> same region (or when the user opts in), so multi-column body text
> can never be mistaken for a table." — the MVP-7 known limitation.

> "Section-header detection beyond numbered all-caps lines: recognize
> title-case headers (the Highlights box list on a drug label) and
> unnumbered headers by font-size/weight, so section-level moves in
> documents without FDA-style numbering are also caught." — the
> MVP-8 known limitation (a move is only detected when both sides
> have at least two matching numbered headers).

Pick ONE. Keep every future prompt this narrow. Review and run the
output before asking for the next slice.

## 11. Section-level pipeline (`core/section_diff.py`)

The engine's out-of-order section handling (MVP-8) shares one header
detector with a standalone, fully-typed block-level pipeline:

- `extract_reading_order(pdf_path)` — text blocks via PyMuPDF
  `get_text("blocks")`, projected into vertical columns (blocks whose
  x-intervals are separated by a real gutter AND whose y-ranges
  overlap open a column gutter; columns emit left-to-right,
  top-to-bottom within each column).
- `build_section_map(blocks)` — chunks the block stream into semantic
  sections keyed by their numbered headers ("2 DOSAGE AND
  ADMINISTRATION", wrapped titles, "2.1"-style subsections).
- `diff_section_maps(app_map, susp_map)` — diffs strictly BY KEY with
  Google diff-match-patch (word-granularity char encoding): a moved
  section is matched and diffed in place, never producing a
  removed/added cascade; sections on only one side become a single
  `deleted_section`/`inserted_section` entry.

`core/diff_engine.py` projects its token stream onto this module's
block schema for section chunking, so there is exactly one header
detector for both pipelines. Run it standalone:

```
python -c "from core.section_diff import *; print(extract_reading_order('file.pdf'))"
```

## 12. MVP-14 (current): rotated text + bracket-mirror + synthesized-preview fixes

Comparing a real Word-draft-to-PDF pair (a packaging-artwork carton
label converted to a Word document for proofing, vs. the final PDF)
surfaced three bugs, all now fixed:

**Rotated (sideways) text was scrambled into single-character noise.**
Packaging artwork routinely prints a caption sideways — a rotated spine
label ("Tizanidine Hydrochloride Capsules") or a dimension callout
("33 mm") next to a die-line arrow. pdfplumber's word grouping assumes
horizontal text, so every rotated character came back as its own
one-character "word", and the page's line/column ordering (also
horizontal-only) scattered these into whatever body text happened to
sit at a similar page height — producing exactly the reported garbage:
a "critical: added" row reading "e d e se i r n o i l d l u h i s n c
p o a a z r d C i T y H mm 33" instead of readable text.
`core/pdf_extractor.py` now reconstructs rotated text directly from
each character's own PDF text matrix (the matrix maps a character's
local axes to page-space direction vectors, giving a rotation-aware
equivalent of the ordinary (x0, top) sort pdfplumber uses for
horizontal text) — verified against the reported file: "Tizanidine",
"Hydrochloride", "Capsules", "33", "mm" now reconstruct as correct
standalone words, carved out as their own reading-order block so they
can never re-contaminate surrounding body text.

**Every PDF word had ")" silently rewritten to "(".** MVP-9's RTL
bracket-mirroring (a real fix for producers that bake in mirrored
bracket codepoints when rendering Arabic/Hebrew) was being applied
unconditionally to every word, not just genuine RTL text — so an
ordinary cross-reference like "(2.1)" was corrupted to "(2.1(" on
*every* PDF, and any comparison against a DOCX/PPTX/XLSX counterpart
(which never went through this corruption) flagged it as changed. On
the reported file this alone accounted for roughly a third of all
"critical" rows. Bracket-mirroring is now gated per-word on the
existing RTL-script detection (`_text_is_rtl`), so it still fires for
genuine RTL parentheticals but never touches ordinary LTR text.

**DOCX/PPTX/XLSX preview text visually ran words together.**
`core/document_extractor.py` lays out these formats using a rough,
Qt-free per-character width estimate (no real font metrics available
there); `ui/document_panel.py`'s synthesized preview then drew each
word at its *real* rendered width with no compensation, so an
all-caps/bold heading (whose real glyphs run wider than the flat
estimate) rendered with adjacent words overlapping into unreadable
noise — "HIGHLIGHTS OF PRESCRIBING INFORMATION" showed as
"HLIGHTSOF PRESCRIBINGINFORMATION". `_draw_synthesized_page` now
measures each word with `QFontMetricsF` and scales it to fit *exactly*
inside its own already-assigned box, guaranteeing no overlap regardless
of the estimate's accuracy on any given machine/font; the layout
estimate itself (`core/document_extractor.py`) was also improved from
one flat per-character constant to a small per-character-class weight
table, so the correction needed at render time stays modest. The same
Unicode normalization PDF extraction already applied (NFKC, ligatures,
zero-width chars, NBSP) is now applied to DOCX/PPTX/XLSX text too, for
the same cross-format comparison consistency.

Verified: all existing smoke tests still pass unchanged, and re-running
the full pipeline against the reported real file pair dropped the
result count from 238 (mostly scrambled/corrupted noise) to 157, all
now readable, legitimate content differences.

## 13. MVP-15 (current): missing-text marker sized to the insertion point, not the other document's whole bbox

A pure insertion/deletion (`added_in_suspect` / `removed_in_suspect`)
has no Location on the side missing the text, so `main.py` drew a
dashed "missing" marker there -- but it did so by mirroring the OTHER
document's full bbox verbatim onto this one. For a whole added
sentence, that meant drawing a sentence-sized highlight box on the
document that doesn't have the sentence, using coordinates from an
entirely different layout -- it landed on unrelated content and looked
like a mistake, not a marker.

`core/diff_engine.py`'s `DiffItem` now carries `approved_caret` /
`suspect_caret`, populated only when the corresponding `*_location` is
`None`: `_caret_location()` finds the real neighboring token in THAT
document's own stream (the word right before the insertion point, or
right after if there's no previous word) and sizes the marker to that
word's own estimated character width -- never more than about one
character wide (floored at 3pt so it's never invisible). `main.py`'s
`on_result_activated` uses this caret when available, falling back to
the old other-side-bbox behavior only when neither neighbor has real
coordinates (e.g. an empty document).

Verified against a real Word-draft-vs-PDF pair: all 118 one-sided rows
got a proper caret (widths 3-7.5pt -- genuinely one-character scale),
with a fixed edge case where a caret anchored at the very left page
margin used to collapse to zero width.

## 14. MVP-16 (current): RTL character/word order corrupted by embedded LTR runs

A real Arabic packaging-artwork comparison surfaced two related bugs,
both in how `core/pdf_extractor.py` handles a Latin/numeric run
embedded inside RTL (Arabic) text -- a batch code, PIN code, or
acronym glued to Arabic words with no space between them, which
pdfplumber routinely does when there's no real gap to split on:

**Word order:** pdfplumber occasionally splits ONE code into multiple
word-FRAGMENTS purely from a font/kerning difference -- a real observed
case: the same batch code came back as one word "0-31" from one PDF but
as two fragments "0" and "-31" from the other, independently-typeset
PDF. The RTL line-sort used to reverse EVERY word's position by
descending x-coordinate, including that fragment pair, turning "0-31"
into "-31 0". `_order_body_units`/`_table_units_for` now group each
line into direction-runs first (`_sort_line_words`): an RTL run still
reverses, but a run of words with no RTL letters at all (digits, Latin,
punctuation-only) keeps its own natural ascending order, however many
fragments pdfplumber split it into.

**Character order:** reversing a whole merged word's characters (the
previous `_fix_rtl_word_chars`, a blind `text[::-1]`) corrupts any
embedded LTR/digit run glued to the Arabic part with no space: a real
observed case turned the acronym "MIDC" into "CDIM", and would turn
"210" into "012". `_fix_rtl_word_chars` now applies a simplified
Unicode Bidi Algorithm: each character is classified rtl/ltr/neutral,
neutral punctuation resolves per rules N1/N2 (matching its flanking
runs, defaulting to RTL when they differ), the resulting runs are
reversed as a SEQUENCE, and only an RTL run's own characters actually
reverse.

**Tokenization boundary:** even with both fixes above, the split-vs-
merged batch code ("0"+"-31" vs "0-31") still didn't match at the
WORD level -- correct content, correct order, but a different number of
tokens. `build_comparison_tokens` gained a second, independent stitch
rule (`_is_code_fragment_split`) alongside the existing line-end-hyphen
rule: a run of adjacent word-fragments on the SAME line, touching (near-
zero gap), where EVERY fragment contains only digits and connecting
punctuation (never a letter), re-stitches into one token. Deliberately
narrow -- it can only ever merge pieces of a code/number, never two
genuine words, and never fires across a real inter-word gap.

Verified against the real reported file pair: the batch-code row that
showed "-31 0" (wrong order) and the "MIDC"->"CDIM" corruption are both
gone, and the split/merged tokenization mismatch that remained even
after the order/character fixes now resolves to a clean match. TEST 11
in `dev_tests/test_extraction_diff_smoke.py` locks in both fixes plus
two safety-guard checks (real adjacent words never merge; numbers
separated by a genuine gap never merge).

## 15. MVP-17 (current): column-gutter width now adapts to the page's own font size

A real Draft-vs-Approved comparison (a compact, small-font folded
patient leaflet) came back with 77 "critical" rows, almost all of them
nonsensical -- e.g. a side-effect bullet "• Dolor" (approved) paired
against a completely unrelated sentence about the drug's composition
(suspect), repeated across dozens of rows. Tracing this to its root:
the suspect PDF genuinely has two side-by-side columns (the tail of the
"Adverse Reactions" section running alongside the start of "Storage" /
"Composition" / manufacturer info, a common space-saving layout for
folded leaflets) separated by a real, completely unambiguous 10pt
gutter -- ZERO of the page's 59 rows had any word crossing it. But
`MIN_COLUMN_GUTTER_WIDTH` (a flat 12pt, tuned against a different,
larger-font document) rejected it as "not wide enough to be a real
gutter," collapsing the whole page to row-major order and interleaving
the two columns' text line-by-line. This document's actual word
spacing was under 3pt -- a fixed point-size threshold tuned for 10-12pt
body text cannot be correct for every font size a real document uses.

`core/pdf_extractor.py`'s column-gutter minimum now scales with the
PAGE'S OWN measured word spacing (`_typical_word_gap`, the median
within-line adjacent-word gap) instead of one constant: a candidate
gutter must be at least `GUTTER_WIDTH_MULTIPLIER` (4x) that page's
typical gap, floored at `MIN_GUTTER_WIDTH_FLOOR` (6pt) so a page with
too little data to measure never gets an unreasonably small threshold;
`MIN_COLUMN_GUTTER_WIDTH` (12pt) remains the fallback when a page has
too few measurable gaps at all. The existing row-crossing-count check
(a candidate gutter must still be empty across nearly every row) is
unchanged and remains the primary defense against false positives --
this only widens what candidate WIDTHS are eligible to be checked.

Verified against the real reported file: the two real columns are now
correctly detected (bands `[(110, 304), (313, 507)]`), and the
suspect-side critical count dropped from 77 (almost entirely cascaded
misalignment noise) to 25, every one of them now a genuine content
difference (a title change, degree-symbol notation, address/
manufacturer details, a genuine missing letter) -- the adverse-
reaction bullet list, once correctly column-ordered, now aligns
cleanly and its differences correctly downgrade to the "typo" tier
(a bullet-spacing formatting nit repeated across ~40 items, not a real
discrepancy). TEST 12 in `dev_tests/test_extraction_diff_smoke.py`
locks this in with a deterministic, hand-built reproduction of the
exact real-world geometry (a 10pt gutter, sub-3pt word spacing) plus a
control assertion confirming the old fixed threshold really would have
missed it.

## 16. MVP-18 (current): highlight coordinates offset on PDFs with a non-(0,0) MediaBox origin

After MVP-17 fixed the column-reading-order cascade above, the SAME
file pair's highlights on the Suspect panel were still visibly
"deviated" -- landing a consistent, small distance away from the
actual text, on every row, only on the Suspect side. Tracing a
specific case (`'FULVESTRANT'`) confirmed the extracted word's raw
coordinates were completely correct -- they matched the real word's
position in pdfplumber's own coordinate space exactly. The bug was one
level down: this suspect PDF's MediaBox has a non-zero origin,
`(-9, -9)` (professionally-produced packaging artwork routinely
defines a bleed box this way), and pdfplumber's raw `x0`/`top` are
reported directly in that native, offset coordinate space -- but
PyMuPDF's rendering (`ui/document_panel.py`'s `render_pdf`, which
these coordinates get overlaid onto for highlighting) always
normalizes so the RENDERED page starts at `(0, 0)`, regardless of the
underlying box origin. Every highlight was landing exactly `(9, 9)`
points away from the real text -- confirmed by cross-checking against
PyMuPDF's own `page.get_text("words")` for the same word, which came
back at `(248.32, 531.11)` where pdfplumber reported `(239.32, 540.03)`
-- a difference of exactly the MediaBox's own origin.

`core/pdf_extractor.py`'s `extract_words()` now subtracts each page's
own top-left corner (`pdfplumber_page.bbox[0]`/`[1]`) from every
word's coordinates at extraction time, normalizing them to the same
`(0, 0)`-origin space PyMuPDF renders in. A PDF with a normal `(0, 0)`
origin (the common case, and the Original/Approved side of the
reported pair) is completely unaffected -- the subtraction is a no-op.
The reading-order algorithms (column bands, table detection, RTL sort)
are untouched, since they only ever compare positions RELATIVE to each
other within one page; an absolute origin shift doesn't affect any of
those comparisons, so this fix is applied at a single point, right
before each `Word` is constructed.

Verified: the corrected coordinates for the real reported case now
match PyMuPDF's own text coordinates to within 0.08pt (sub-pixel
rounding). TEST 13 in `dev_tests/test_extraction_diff_smoke.py` locks
this in with a synthetic PDF whose MediaBox is deliberately offset by
`(-20, -30)`, asserting every extracted word's coordinates equal
pdfplumber's raw value minus the page's own origin.

## 17. MVP-19 (current): out-of-order section matching now works without numbered headers

MVP-8's out-of-order section matching -- match each section by its
header, regardless of stream position, so relocated content is a
low-severity "reordered_section" instead of a critical cascade -- only
ever activated for documents using FDA/EMA-style NUMBERED headers
("1 INDICATIONS AND USAGE"). A real Draft-vs-Approved comparison (a
Discover Cardmember Agreement) has real, addressable sections
("CARDMEMBER AGREEMENT", "INTEREST CHARGES", "ARBITRATION") but NO
leading section numbers at all, so the whole document fell back to a
linear word-stream diff. The suspect PDF also combined two genuinely
unrelated pieces of content onto one oversized print-proof sheet (a
"Billing Rights" insert alongside the main Agreement, a real print-
production convention), read column-major in a different relative
order than the approved reference -- so the stream diff's very first
alignment was already wrong, cascading into the document control code
("CM.TL24K.LIN.0215") getting paired against an unrelated 800-word
paragraph, and 76+ more nonsense pairings after it.

`core/section_diff.py`'s `build_section_map` gained a fourth header-
detection rule: a short (<= 6 word), ALL-CAPS line whose tokens are
PURE LETTERS ONLY (no digits, no embedded dots) is now also recognized
as a section header, even with no leading number. The letters-only
restriction is what keeps this from ever mistaking a document control
code for a header -- "CM.TL24K.LIN.0215" is nominally all-caps too,
but its tokens mix digits and bare dots the way no real title ever
does. `core/diff_engine.py`'s `_match_sections` now also title-anchors
two UNNUMBERED headers against each other (at a stricter similarity
threshold than the numbered case, since there's no independent number
to confirm the pairing) -- so "CARDMEMBER AGREEMENT" pairs with
"CARDMEMBER AGREEMENT" regardless of how differently the two
documents' bodies got laid out.

**Getting this right without disturbing the numbered-header path (already
proven correct, including for the RTL/Arabic document from MVP-17/18)
took a second pass**: the first version of this fix broke TEST 9 by
splitting a document's own two-line title/caption ("FULL PRESCRIBING" /
"INFORMATION" -- FDA labels' standard cover caption) into a spurious
extra section, because both physical lines independently looked like a
valid unnumbered header. The fix (`_looks_like_unnumbered_header`,
used as a guard by its own caller): a candidate line is only accepted
as a genuine header when NEITHER its immediately preceding NOR its
immediately following line also has the header shape -- a real section
header sits between ordinary body text (or the document's start/end)
on both sides, while a multi-line caption fragment always has ANOTHER
caption-shaped line touching it. TEST 14 locks in both the new
capability (unnumbered sections matched across a different relative
order, reorder-demoted, zero criticals) and this specific guard (the
wrapped-caption false positive, and the control-code-immediately-
before-a-real-header case), and the full existing suite -- including
TEST 8/9's numbered-header scenarios -- passes unchanged, confirming
the numbered-header path is untouched.

**Known remaining limitation, found while verifying this fix**: the
reported document's own ARBITRATION section has a column layout that
still interleaves incorrectly EVEN WITH the adaptive gutter fix
(MVP-17) -- present identically in both the approved and suspect PDFs,
so it's a pre-existing extraction issue orthogonal to this milestone's
section-matching fix, not something introduced by it. Verified: the
section IS now correctly matched by title (previously it wasn't
detected as a section at all), but its own internal word order is
still wrong on a page whose column gutter apparently isn't cleanly
detected by the current heuristic. A candidate for a future, narrower
slice.

## 18. MVP-20 (current): a page can stack MULTIPLE incompatible column structures, and a borderless table isn't flowing text

Following up on MVP-19's known limitation directly: the SAME reported
document's ARBITRATION section was still interleaving three columns'
text into nonsense, even with MVP-17's adaptive gutter width. Two more
reading-order bugs, both now fixed, plus one that was tried, measured,
and deliberately reverted after making things worse.

**Multiple column structures on one page.** MVP-17 made the gutter-
width THRESHOLD adapt to a page's own word spacing, but still measured
gutters once for the WHOLE page. A single page can legitimately stack
a single-column clause, a 2-column glossary, AND a 3-column
arbitration clause -- three genuinely different structures, all
present on this exact page. The single-column clause's lines span the
full content width, so they "cross" any candidate gutter positioned
anywhere in the middle of the page -- including a gutter that is
completely clean (zero crossings) within the 3-column section that
actually needs it, which is exactly what was happening: isolating just
the arbitration rows and re-measuring confirmed a real, unambiguous
3-column split was there all along, invisible at the page level purely
because unrelated single-column content elsewhere on the SAME page
kept "crossing" that x-range. `core/pdf_extractor.py`'s
`_split_into_zones` now breaks a page's rows into zones at likely
section-header lines (reusing the exact header-shape checks
`core.section_diff.build_section_map` uses to find sections in the
first place, exported as `is_likely_header_line`), and column-gutter
detection runs separately within each zone. `GUTTER_WIDTH_MULTIPLIER`
was also nudged down from 4.0 to 3.5 -- the smallest change that let a
real, completely clean 8pt gutter (on a zone whose own typical word
gap was 2.3pt) clear the bar.

**A borderless "label: value" table isn't flowing body text.**
Column-major reading (read all of one band, top to bottom, before the
next) is only correct when a typesetter divided the width into roughly
EQUAL columns. A borderless "label: value" table -- no ruling lines,
so `_detect_page_tables` correctly never claims it -- produces the
same kind of x-gap a real column gutter does, but its bands are
wildly UNEQUAL (a narrow label column next to a wide explanation
column). Reading it column-major joined every label together, then
every explanation together: "Governing Law" / "Severability" /
"Enforcing this Agreement" each landing before an unrelated LATER
row's explanation instead of its own. `_bands_are_column_flow` now
requires a zone's narrowest band to be at least 40% of its widest
before treating it as column-major; below that, the zone falls back to
its own natural row order (each row's own bands read left-to-right),
which is exactly right for a label/value layout and doesn't affect a
genuine, roughly-equal-width multi-column section at all.

**Tried and reverted: splitting a "line" on a large horizontal gap.**
The same document's suspect PDF was an oversized print-proof sheet
combining two documents side by side (see MVP-19), and two of their
running heads ("MAKING PAYMENTS" / "OTHER IMPORTANT INFORMATION")
landed at the exact same page height, ~540pt apart horizontally --
concatenating into one nonsense section title neither document
actually has. Two fixes were tried for this: rejecting a bare page
number immediately followed by a date/time-stamp footer as a header
trigger, and splitting `core.diff_engine._group_token_lines` on an
implausibly wide horizontal gap between same-height tokens. Both were
verified, via direct before/after A/B counts on the real document, to
make the OVERALL result worse, not better -- e.g. the line-split alone
took the result from 75 discrepancies to 108, by scrambling ~40
unrelated lines in a dense multi-column "term: definition" section
elsewhere in the SAME document that legitimately has within-line gaps
in that same range. Both were reverted; a narrower, confirmed-harmless
version of the footer guard (rejecting a date/time-shaped token
specifically as a WRAPPED multi-line title's continuation, not as an
outright rejection of the header line itself) was kept. The original
symptom -- one garbled section title combining a print-shop footer
stamp with two real running heads -- remains a known, narrower
limitation, preferred deliberately over trading it for wider damage.

Verified: all 51 existing test assertions pass unchanged (including
TEST 5's table handling and TEST 9's numbered-header wrap detection,
neither of which this milestone touches), and the real reported
document's ARBITRATION section -- previously an unreadable interleave
of three columns' text -- now extracts as a clean, complete, correctly-
ordered clause. TEST 5 (tables) and the full suite were specifically
re-verified after this milestone's zone-splitting change, since it
touches the same `_order_body_units` table-interleaving code path.

## 19. MVP-21 (current): MVP-20's zone-splitting shipped two regressions -- both fixed

MVP-20's per-zone column detection was verified against the ONE real
document that motivated it, but two more real documents -- an Arabic
drug-label pair and a large multi-panel credit-card-agreement proof
sheet -- immediately exposed two regressions it introduced, severe
enough to corrupt reading order across BOTH documents (Arabic
included, despite that path being untouched by MVP-20's own changes)
and reported as "everything is gone, no more correct comparison even
in arabic too." Root-caused and fixed both; full suite re-verified
against the actual reported files, not just synthetic tests.

**Regression 1: the zone-boundary detector had no real validation.**
`core.section_diff.is_likely_header_line` (the check `_split_into_zones`
uses to decide where a page's column structure might change) accepted
ANY line whose first token was a bare 1-3 digit number, optionally
with a trailing "." or ")" -- with no check on what followed. That
shape matches a genuine FDA-style header ("1" alone on its own line),
but it ALSO matches an ordinary numbered list item ("1. Within 30 days
of receiving your letter, we must tell you...") and a bare drug-dose
number ("150 mg", "75 mg" -- the first token in x0-ascending order on
an RTL line, since `_split_into_zones` builds its probe text by x0
regardless of reading direction). Both real documents are dense with
exactly this shape: the credit-card agreement's numbered clauses and
the Arabic label's dosage table each forced a NEW zone boundary on
nearly every line, fragmenting whole sections into zones of 1-2 rows.
With that little data, `_adaptive_min_gutter` can't measure a zone's
own word spacing (needs >= 5 gap samples) and falls back to a fixed
threshold, which then misread an ordinary wide gap WITHIN one line
(e.g. before a parenthetical aside) as a column gutter -- splitting
that single line's own words into fake columns, stitched back in the
wrong relative order against the next zone. `is_likely_header_line`'s
numbered branch now requires the same evidence
`core.section_diff.build_section_map`'s own numbered-header branch
already required all along (`has_caps_words`): a bare number ALONE on
its line is still accepted outright (the genuine FDA case), but a
number followed by more text on the same line is only accepted when
that tail is itself caps-styled. A numbered list sentence (lowercase
tail) and an Arabic dose line (non-Latin tail, which can never satisfy
the Latin-only caps-token check) are both correctly rejected.

**Regression 2: a real 3+ column page misclassified as a table.**
Independently, `_bands_are_column_flow` (added in MVP-20 to stop a
borderless label/value table from reading column-major) required a
zone's NARROWEST band to be at least 40% of its widest before treating
it as flowing multi-column text -- reasonable for the 2-band label/
value case it was built for, but wrong once 3+ real, independently-
gutter-detected columns exist: the reported proof sheet is an
oversized, four-column layout (page 1272pt wide) where one column was
only ~70pt (a hanging bullet-marker strip in a list-heavy clause) next
to others ~300-470pt wide. The width-ratio check failed on that one
narrow column and collapsed the ENTIRE zone to row-major, reading all
four columns interleaved word-by-word wherever their lines happened to
land at the same height -- producing exactly the nonsense pairings
reported ("What Will Happen After We Receive Your Letter" directly
followed by an unrelated column's "1. The purchase must have been
made..."). A borderless label/value table inherently has exactly TWO
columns (one narrow label, one wide value); a genuine 3rd or 4th
independently-detected column essentially never arises from that
shape. `_bands_are_column_flow` now skips the width-ratio check
entirely once 3+ bands are found and always treats the zone as
column-flow, leaving the 2-band check (and TEST 15's label/value case)
untouched.

Verified directly against both real reported files (not just
synthetic data): the Arabic label's page 1 now extracts as a single
zone again (34 rows, 0 spurious splits, matching pre-MVP-20 behavior);
the proof sheet's "What Will Happen After We Receive Your Letter" is
now immediately followed by its own section's actual text ("When we
receive your letter, we must do two things: 1. Within 30 days...")
instead of jumping into an unrelated column. Full suite: 54/54
assertions still pass unchanged, including TEST 15 (the per-zone /
column-flow tests this milestone's fixes sit directly next to).

## 20. MVP-22 (current): three more real-document regressions, found by not stopping at "the synthetic tests pass"

MVP-21 fixed the two regressions its own test files exposed; continuing
to test that fix against the SAME two real files -- an Arabic drug
label and a large multi-panel credit-card-agreement proof sheet --
immediately surfaced three MORE real regressions, all root-caused and
fixed the same day. The common thread: every piece of multi-column /
multi-cell reading-order code in this file was originally written and
tuned LEFT-TO-RIGHT-first, with RTL patched in only at the word and
line level (character order, word order within one line) -- never at
the structural level (which column comes first, which side is the
"label", how confidently a small sample of rows can be trusted to
reveal real column structure). Real, sufficiently complex documents
exercised all three gaps at once.

**A label/value record's own value cell can wrap across two
independent sub-columns.** A real credit-card Fees table's "Late Fee"
row has a "for Purchases" explanation immediately next to a parallel
"for Cash Advances" explanation -- two genuinely independent
paragraphs sharing one table row, each wrapping onto its own following
line(s). MVP-20/21's row-major fallback (for the "narrow label, wide
value" case) read this literally per PHYSICAL line, left to right:
when the two sub-columns wrapped to a *different* number of lines for
the same record (verified against the real file), a later physical
line mixed the TAIL of one record's sub-column with the HEAD of the
next record's label -- "We will charge you a Returned Check Fee...six
billing periods. Otherwise, the fee is $35..." splicing two unrelated
fee explanations into nonsense.

The fix (`_order_label_value_zone`) partitions a zone's rows into
per-record groups at each row carrying a word in the label column (the
first physical line of every record always has one; every wrapped
continuation line never does -- a purely geometric signal), then looks
for a genuine internal split within JUST that one record's own value
words. This deliberately does NOT reuse the general
`_detect_column_bands` row-crossing-count sweep: that sweep's crossing
TOLERANCE floors at "at least 1 row may cross" no matter how few total
rows there are, which is fine for a whole page but statistically
meaningless for the 2-3 lines one record typically has -- it was
fragmenting a real record into up to 8 nonsense slivers. Instead,
`_consistent_internal_gutter_x` requires EVERY multi-word row in the
record to independently have a gap of at least the page's own adaptive
gutter width, AND for all those rows' gap positions to overlap into
one shared x-range -- unanimous agreement across every line, not a
majority. If found, that one record's value words split into two
sub-columns, each read fully (top to bottom) before the other; if not
(the common case -- a single flowing paragraph), the record reads in
its own natural row order.

**Multi-column reading order was never reversed for RTL.** A real,
wide (1272pt-1190pt) RTL proof sheet has 4-5 genuine, independently-
detected text columns -- but the code that decides "read column 0
fully, then column 1, then column 2..." always iterated column indices
ascending (left to right), regardless of the page's own RTL flag, at
every level this milestone touched: the whole-zone flowing-column
order, AND (for a label/value record) which side is the "label"
column and which sub-column reads first. On an RTL page the FIRST
logical column is the RIGHTMOST one, not the leftmost. The observed
failure: a Storage-section sentence ("...store below 30°C, away from
direct sunlight") landed spliced into the middle of an unrelated
Overdose section's sentence two columns away, because the Overdose
column was read before the Storage column finished even though,
visually, Storage sits to the right and should be read FIRST. Fixed by
flipping the column-index assignment (and the label-side / sub-column-
side choice) whenever `rtl=True`, while keeping every column's own
real (lo, hi) x-range intact for spatial lookups (e.g. a table's band).

**A decorative bordered box around one paragraph, misread as a real
table.** `_detect_page_tables` only claims RULED tables (a deliberate
choice -- see its own module note on why a borderless-table heuristic
was rejected), and on the real Arabic file it found a completely
legitimate-looking 6-row x 3-column grid: a decorative border drawn
around ONE flowing "Overdose" paragraph had incidental ruling lines
that `page.find_tables()` read as real column dividers. Every "row" of
this fake table populated EXACTLY ONE of its 3 columns -- never two at
once -- which is the opposite of what a real table's rows do (a real
table's whole point is that related data spans multiple columns
together in one row). Splitting this paragraph into "cells" positioned
it using the TABLE's own (band, top) placement instead of the
surrounding paragraph's, and since this fake table's bbox geometrically
overlapped a separate, genuinely adjacent body paragraph (Storage), the
two spliced together. Fixed with `_looks_like_real_table`: any detected
table where fewer than 30% of its rows populate more than one column
at once is rejected, and its words fall back to ordinary body-text
handling instead.

Verified directly against both real reported files (not just synthetic
data): the Arabic pair's diff count dropped from 70 (mostly scrambled
noise from all three bugs compounding) to 4 genuine differences (a
missing word, a changed digit, two minor spacing typos); the English
proof sheet's Fees table rows and previously-spliced numbered-list
sections now read as complete, coherent sentences with no
cross-contamination between unrelated rows/columns. Full suite: 66/66
assertions pass, including 7 new ones (TEST 17) that reproduce all
three bugs directly (not just re-running the real files) with a
control for each, proving every fix is actually load-bearing and none
of them regress the label/value, RTL, or real-table behavior TEST 12/
15/16 and TEST 5 already lock in.

## 21. Background extraction on load

Text extraction now starts the moment a document finishes loading
(`workers/extraction_cache.py`), in two worker processes so the
Original and Suspect extract in parallel without slowing the GUI.
Compare reuses the finished result, or waits only for whatever is still
running (status bar: "Finishing extraction of ..."), then diffs.
Results are cached per file by (path, modification time, size), so
re-loading an unchanged file is instant and an edited file is
re-extracted. A file that fails to extract (e.g. a scanned PDF) is
reported in the status bar right away; Compare shows the full error.

Measured on a real 2-PDF pair: extracting both one after the other on
Compare took 7.7s; with background extraction both were ready ~5s
after loading, and Compare itself took 0.35s.

## 22. Superscripts and subscripts are kept and compared as-is

A PDF has no superscript flag -- a raised/lowered glyph is just a
smaller character with a shifted baseline. pdfplumber flattened
superscripts into their neighbor ("2" + raised "4" read as "24",
identical to a real 24) and dropped subscripts lowered more than 2pt
onto a line of their own ("h₂so₄" came out as "h so" plus a stray
"2 4").

`core/pdf_extractor.py::_mark_script_chars` now finds glyphs that touch
a noticeably larger glyph (40-85% of its size) with a shifted baseline
(>= 0.25pt; real subscripts were seen lowered by only 0.48pt), tags them
superscript/subscript, and moves them onto their base's line. Smaller
glyphs on the SAME baseline (small caps) stay normal text, but also
join their line, so "S"+"MALL" is one word. `core/script_text.py` then
writes these runs with Unicode forms ("2⁴", "H₂O", "Cₘₐₓ", "[¹⁴C]");
a run with a letter/digit that has no Unicode form becomes `^{...}` /
`_{...}` (e.g. `t_{1/2(lambda)z}`), and a raised run of punctuation
only stays plain. Word (run superscript/subscript formatting),
PowerPoint (run `baseline` offset) and Excel (whole-cell `vertAlign`)
produce the same text, so a DOCX draft matches its PDF.

Comparison treats them as content: normalization no longer folds them
to plain text (NFKC would turn "⁴" into "4"), and a superscript or
subscript digit is part of its number, so "2⁴" vs "24" and "H₂O" vs
"H2O" are critical numeric changes. TEST 19 locks this in.

## 23. Word documents are laid out by Microsoft Word

DOCX files used to be drawn by the app itself from python-docx: one
endless synthesized page, no table borders, approximate positions --
nothing like the real document. There is no reliable way to reproduce
Word's layout (pagination, tables, columns, text boxes, fonts) outside
a real layout engine, so `core/office_convert.py` now has one do it:

- **Microsoft Word** via COM automation (`pywin32`), in a private,
  invisible Word process that never touches the user's open Word
  windows. Tracked changes are exported as the final text without
  markup; comments are not included.
- **LibreOffice** (`soffice --headless`) if Word isn't installed.
- Neither installed: the old approximate layout, and the panel says so.

The resulting PDF is what the viewer shows AND what is extracted and
compared, through the normal PDF pipeline -- so a Word draft now has
its real pages and bordered tables, and highlights land exactly on the
text. Conversion runs in the background when the file is loaded (the
panel shows "Laying out ... with Microsoft Word..." and Compare waits),
and the PDF is cached under `%LOCALAPPDATA%\DocForensicsAnalyzer\converted`
by (path, modification time, size), so the background extraction and
any later re-open reuse it. A corrupt or password-protected document
reports Word's own error instead of hanging on a hidden dialog.

Measured on a 46-page label (`sample 2 (1).docx`): first layout ~7s
(most of it Word starting), cached re-open ~1ms; the page with Table 3
renders identically to Word, and its cells extract row by row.

## 24. Faster Word layout: warm background Word, lighter export, parallel files

Measured on the 46-page `sample 2 (1).docx`: starting Word ~2s,
opening the file ~1-2s, and the PDF export itself the rest (11-18s on
a busy machine). So three changes:

- **Two hidden Word instances start when the app opens** and stay
  open (`core/word_pool.py`); every Word document reuses them instead
  of starting and quitting Word. The window appears immediately; Word
  warms up behind it (2-5s).
- **Both sides are laid out at the same time** -- each instance has
  its own thread and they share one job queue. Two Word files that took
  54.9s one after the other took 26.0s together.
- **Lighter PDF export**: no accessibility tags, bookmarks, document
  properties or print-quality images (`_WORD_EXPORT_OPTIONS` in
  `core/office_convert.py`), ~25% faster export and a 42% smaller PDF,
  with identical extracted text and word positions (13,965 words
  compared). Text in fonts Word can't embed stays real text instead of
  being turned into a picture.

Robustness: a Word instance that crashes is restarted and the document
retried once; a layout running over 10 minutes has its Word terminated
and is reported as an error; closing the app quits both instances, and
a Windows job object terminates them even if the app itself crashes or
is killed. Background processes (text extraction) that need a PDF that
isn't cached yet still use a one-shot Word.

## 25. Independent viewers, "Lock views", and two-way navigation

- **Each viewer zooms on its own.** The shared toolbar zoom is replaced
  by −/%/+/Fit controls on each viewer; pinch and Ctrl+wheel also zoom
  only the viewer they happen in. Zoom is anchored where you zoom (the
  point under the cursor/fingers stays put; buttons keep the view
  center), and one Ctrl+wheel notch in then out returns to the same
  level. The old mirroring copied raw scrollbar values, which is why
  zooming one side threw the other onto a different page; it is gone.
- **Lock views** (toolbar checkbox) keeps both viewers in step: the
  same zoom, and scrolling one moves the other to the same TEXT (via
  the comparison's word alignment; before a comparison, the same page
  position). ~10 ms per scroll step on a 46-page document. Unlocked,
  the viewers are fully independent.
- **Results table → viewers:** clicking a row, or moving through rows
  with the arrow keys, navigates both viewers; the other viewer first
  takes the zoom of the viewer used last, so both show the difference
  at the same scale.
- **Viewer → results table:** clicking a highlighted difference on a
  page selects its row (scrolled into view) and navigates both viewers.
  If a table filter hides that row, the status bar says so.
- **Viewer → other viewer:** clicking ordinary text marks the word in
  blue and takes the other viewer to the same word, even when the
  layouts differ entirely (a 46-page Word file vs a 2-page print
  sheet). It never guesses (`core/token_map.py`):
  1. a word in a run of 3+ words the comparison matched in sequence
     maps to its partner;
  2. otherwise its surrounding 5-word phrase is looked up in the other
     document -- used if it occurs there once, or once between the
     matched words bracketing the click. This finds text the comparison
     itself paired wrongly (a print sheet where a figure splits a
     section);
  3. otherwise the other viewer stays put and the status bar says the
     text isn't there. (An earlier version jumped to the nearest
     matched word up to 400 words away -- e.g. from "absorption" to an
     unrelated "and" in a figure.)
  Checked on 1,800 random words across three real document pairs: 0
  jumps to a different word. A word identical in both documents always
  counts as text, even under a difference's line-wide highlight.

How: `core.diff_engine.diff_documents(..., matches=...)` records the
word pairs the matcher found identical; `core/token_map.py` turns them
into click-point → word → partner-word lookups; `DiffWorker` returns
both as a `ComparisonResult`. `dev_tests/manual_e2e_compare_check.py`
covers all of this (steps 6-16) and now runs to completion (its old
test pages had too little text and a modal error dialog blocked it).

## 26. Section pairing on print sheets: moved-text pass, repeated headers, prose severity

Comparing a 46-page Word label against its 2-page print sheet reported
the second half of section 12.3 (960 words that ARE on the sheet) as a
single "minor" text change against the figure label "90% Cl" -- a real
move shown as the wrong kind of difference, and hidden by "Ignore minor
and below". Three causes, three fixes (`core/diff_engine.py`,
`core/section_diff.py`):

- **Section chunking can be fooled by layout.** On the sheet, the
  figure's axis label "Renal Impairment AUC :" has the shape of a
  heading, so everything after it became one 3,050-word chunk, paired
  by content with the Word file's MEDICATION GUIDE. Instead of chasing
  every such heading shape, `_diff_by_sections` now has a second pass:
  everything the section pairs leave unexplained is searched for runs
  of 6+ identical words present on both sides (`_find_moved_blocks`,
  order-independent). Fragments close together on both sides form one
  moved passage (`_moved_passages`), compared as a unit -- an edit
  inside moved text is one "Text Changed" row -- and reported as one
  low-severity "Reordered" row; the sections are then compared again
  without it (reusing the first pass wherever nothing moved).
- **A repeated header merged unrelated chunks.** Sections were kept in
  a dict keyed by header text, so a table-of-contents line and its
  section, or three separate "Manufactured by:" blocks, became one
  chunk. `build_section_list` keeps each occurrence separate
  (`build_section_map` still merges them for the block-level
  pipeline), and same-titled chunks pair occurrence by occurrence.
  A "Reordered Section" row now requires the two headers to actually
  match (renumbered titles count), so a content-only pairing can't
  claim a section move.
- **Prose rated as chart labels.** The "axis tick sequence" rule fired
  on any text with five rising numbers, so long removed/added
  paragraphs came out "minor" (901-, 578-, 960-word rows on real
  pairs). It now requires the text to be mostly numbers, and the
  production-slug rule only applies to short texts.

On the real pair: the 12.3 text is a moved passage, "absorption" /
"birth" are matched by the comparison itself, and the largest remaining
addition is Figure 2/3's text (real text in the PDF, an image in the
Word file). Other real pairs: unchanged or more precise (a destination
proof's extra copies of sections 17.x are now reported as added instead
of silently demoted). Comparison time is unchanged (5.5s for the
46-page pair). TEST 20 covers all three fixes.

## 27. Interactive deviation list and mass accept/reject

`ui/results_panel.py` is now a review checklist rather than a plain
table:

- **Categories.** Every difference is sorted into Added, Removed,
  Number/dosage, Wording, Capitalization, Punctuation/spacing, Spelling
  or Moved (`core.diff_engine.category_of`, derived from the same facts
  as the severity ladder), shown as a column and as a row of filter
  chips with counts.
- **Filters.** Category chips, "Ignore minor and below", a text search
  and "Hide reviewed". Filters change only the view; the page
  highlights follow the list, so hiding a category also clears it from
  both documents.
- **Review decisions.** Accept (an intended change) / Reject (an error
  to fix) / Reset, for every selected row: buttons, the A / R / U keys,
  or right-click. Each decision stores the reviewer (toolbar
  "Reviewer:" field, defaulting to the Windows user name) and a UTC
  timestamp, shown in the row's tooltip. Accepted differences drop off
  the page highlights.
- **Mass actions.** "Group identical" folds differences with the same
  category and text into one row with a count (×400); one decision
  covers the group. Without grouping, right-click offers "Accept/Reject
  all N identical".

Checked in `dev_tests/manual_e2e_compare_check.py` steps 17-20
(categories, filters driving the highlights, decisions with reviewer
name, Hide reviewed, 5 identical changes decided at once).

Not yet: formatting differences (font size, bold/italic) are not
compared, so there is no "formatting" category to filter.

## 28. Professional UI pass

- **Theme** (`ui/theme.py`): one token set per mode -- a warm off-white
  light theme and a near-black dark theme, neutral surfaces, 1px
  borders, one blue accent, no gradients or shadows -- applied as a
  Fusion palette plus one stylesheet. View > Theme: System (follows the
  Windows setting live), Light, Dark. Severities are GitHub-style labels
  (colored text on a faint tint); "moved" is purple on the list and on
  the pages.
- **Window structure** (`main.py`): menu bar (File / View / Review /
  Help), a slim toolbar (Compare as the one primary button, Previous /
  Next difference, Lock views / Highlights / Blink toggles, Reviewer),
  a status bar with a busy indicator (layout, extraction, comparison),
  and the deviation list as a dockable "Deviations · N" pane that can be
  moved, floated or closed (View > Reset Layout brings it back).
  Window size, pane layout, theme, reviewer name (only when typed) and
  the last folder are remembered. Blinking highlights are now off by
  default.
- **Keyboard** (Help > Keyboard Shortcuts, F1): Ctrl+O / Ctrl+Shift+O
  open, F5 compare, F8 / Shift+F8 next / previous difference, A / R / U
  in the list, Ctrl++ / Ctrl+- / Ctrl+0 zoom the last used viewer,
  Ctrl+L lock, Ctrl+H highlights, Ctrl+F find in list.
- **Viewers** (`ui/document_panel.py`): a header with the role, file
  name (middle-elided, full path on hover), format and page count, and
  Open...; a navigation strip with a page number box, page arrows and
  zoom controls; pages drawn on a neutral canvas with a 1px border; an
  empty-state prompt; drag and drop a file onto a viewer to open it.
- **Deviation list**: single-line rows (full text on hover), theme
  colors throughout, dimmed rows once reviewed, category chips disabled
  when a category has no results.

## 29. Speed

The app was taking about 50 seconds from opening a 46-page Word
document to a reviewable list, and single clicks in the list took
seconds. Five changes, no behaviour lost -- the same differences come
back, verified against the previous engine on the real file pairs:

| Step | Before | After |
|---|---|---|
| Opening a document | 2.7s, window frozen | 0.1s |
| Text extraction (46 pages) | ~31s | ~3.6s |
| Compare | ~14s | ~1.0-1.3s |
| Accept / Reject a row | 5.7s | 0.05s |
| Filters, next difference, scroll | up to 0.4s per step | ~0.1s or less |

- **PDF reading is PyMuPDF, not pdfminer** (`core/mupdf_page.py`).
  pdfplumber interprets the page content stream in pure Python; that
  was two thirds of extraction time (8.7s for one 2-page print sheet's
  92,000 characters, which PyMuPDF's C engine reads in 0.25s).
  `MuPdfPage` reads characters and vector drawings with PyMuPDF and
  presents them the way `core/pdf_extractor.py` already consumed a
  pdfplumber page -- including pdfplumber's own word grouping and
  table finder, so reading order, tables and coordinates are unchanged.
  Text painted with fill AND stroke arrives twice from PyMuPDF and is
  de-duplicated (pdfminer reported it once). Set
  `DOCFORENSICS_PDF_ENGINE=pdfplumber` to go back to the old engine.
- **Pages are extracted in parallel** (`workers/extraction_cache.py`):
  a PDF is split into page ranges across `CPU count - 1` worker
  processes (up to 12) and the parts are concatenated in order, so a
  long document uses the whole machine. The page range containing page
  1 also runs the scanned-PDF check.
- **Section matching skips impossible pairs** (`core/diff_engine.py`).
  Pairing 100 approved sections against 270 suspect ones ran 27,000
  full `SequenceMatcher` similarity ratios. `_BodySimilarity` now
  rejects a pair on a length bound and then a word-bag bound (both
  upper bounds on the real ratio) before computing it, and caches
  matchers per suspect section: 14s to 1.0s, with identical results.
- **The deviation list no longer re-measures every row.** Its columns
  were `ResizeToContents`, which re-measures every row on every cell
  update -- one Accept took 5.7s on a few hundred rows. Fixed widths
  for the narrow columns, stretch for the two text columns: 0.05s.
- **Pages are drawn in the background, only where you are looking**
  (`ui/document_panel.py`). Opening a document used to render every
  page to a pixmap up front (2.7s for 46 pages, window frozen). Now
  the page frames are laid out from the page sizes alone and each page
  is rendered by a background thread -- nearest the view first, at a
  resolution matched to the current zoom (25% / 50% / 100%), at most
  two in flight, at most 14 kept -- so memory stays flat on a long
  document.

Verified: the 82-check smoke suite passes with both PDF engines, the
28-check GUI suite passes, and 25 random word clicks all still land on
the same word in the other document.

## 30. Comparator: what changed inside the selected difference

The deviation list says THAT a passage differs. The comparator pane on
the right of the Deviations dock (`ui/comparator_panel.py`) says what
in it differs, which is what a reviewer signs off on:

- **The Original text and the Suspect text**, each with the exact words
  that differ marked -- red for what the Original had, green for what
  the Suspect has. Inside an edited word only the changed characters
  are marked ("Ropinirole" / "Ropinirol"), because marking a whole
  sentence to report one letter is what made the old text columns hard
  to read.
- **In context.** The words around the difference are shown dimmed, so
  a one-word change can be read in its sentence. They come from the
  comparison's own word alignment (`core.token_map.TokenMap.
  context_words`, and `context_at_caret` for text that exists on only
  one side -- the insertion point sits between words), so they are that
  document's real neighbouring words, not a re-read of the file.
- **Invisible differences are made visible.** A space that is itself
  part of the difference is drawn as a middle dot; a spacing-only
  difference says so in words ("Spacing only -- the words themselves
  are identical"), and moved text says "Same text -- moved to a
  different position".
- **Header and summary.** Severity badge, category, the page each side
  sits on, the review decision with reviewer and time, how many times a
  grouped row's difference occurs, and a one-line summary naming the
  changed words ("Ropinirole 5  ->  Ropinirol 50" -- whole words, not
  the single letter inside them).
- **Both ways.** Selecting a row, arrowing through the list, Next /
  Previous difference, and clicking a difference in either document all
  fill the pane. Drag the splitter to resize it; View > Comparator
  hides it. Position and visibility are remembered.

The word/character splitting is `core/inline_diff.py` -- pure text in,
runs of unchanged/changed text out, no Qt -- and concatenating the runs
always reproduces the input exactly, so the reviewer can never be shown
altered text.

Checked in `dev_tests/test_extraction_diff_smoke.py` TEST 21 (what is
marked, spacing-only differences, whole-word summaries, unrelated words
not refined letter by letter) and
`dev_tests/manual_e2e_compare_check.py` step 21 (a real comparison:
"colour" / "color" spelled out in the document's own surrounding words,
one marked run, the decision shown, filled from a viewer click, hidden
and brought back).

## 31. A contents list is compared with the contents list, not with the sections

Reported on a 100-page vs 128-page prescribing-information pair
(`100 pages .pdf` / `100 pages diff.pdf`): the index page's headings
were being compared against the real paragraph headings, filling the
list with differences that were not differences. 71 rows, 3,200 flagged
words, 33 of them "moved". Two independent causes, both fixed.

### Cause 1: a contents line outranked the section it points at

Every header in this document exists at least twice -- once in the
contents list, once as the section itself -- and the leaflet is
repeated down the document, so "6 ADVERSE REACTIONS" occurs about
twenty times on each side. All of those copies score identically on
number and title, and `_match_sections` had no way to say which copy
was which: it broke the tie on the n-th occurrence preferring the n-th
on the other side, which falls apart as soon as one document holds an
extra copy. A contents line (3 words) then claimed the real section
(55 words), so the section's entire body read as deleted and the other
document's as added. 11 such pairings on this file.

The fix is the one the reviewer asked for -- match on the surrounding
text. `_ContextSimilarity` compares the 12 words immediately before and
after each section (`_section_edges`, walking across neighbouring
sections): a contents line sits between other contents lines, the real
section sits between paragraphs of prose, so the right pairing wins
even though both copies score the same on the header. It is a pure
TIE-BREAKER, ranked after the score and before the n-th-occurrence
preference, so it can only choose between equally-scoring pairs and
never promotes a worse number/title/body match. A genuinely moved
section, whose neighbours differ on purpose, is unaffected.

### Cause 2: a heading number drawn in its own box lost its heading

On the original's pages 11-12 the heading "5.5 Interaction with CYP1A2
Inhibitors" is drawn as two pieces: "5." in a box half the height and
4pt lower than the rest, immediately left of "5 Interaction with...".
`_group_into_lines` put the "5." on a line of its own, so the heading
never looked like a heading, the section was never detected, and its
whole body was absorbed into the contents chunk before it -- reported
as one large deletion, with the suspect's properly-detected sections
reported as additions.

`_group_into_lines` already rejoined a small glyph run TOUCHING a word
on the line (a trailing sub/superscript); it now does the same for one
touching on the LEFT, which is what a leading section number is. The
existing code-fragment stitch then merges "5." + "5" back into "5.5",
and the heading is detected exactly as it is in the other document.
Neighbouring lines do not overlap vertically at all, so they are
unaffected.

### Result on the reported pair

| | Before | After |
|---|---|---|
| Differences listed | 71 | 36 |
| Words flagged | 3,202 | 177 |
| "Moved" rows | 33 | 3 |
| Critical rows | 11 | 7 |
| Contents-vs-section pairings | 11 | 0 |

What remains is real: the injected typos ("markaeting", "brdadycardia",
"tdo", "dmg."), hyphens the suspect dropped ("voltage-gated" ->
"voltagegated"), and one table header whose columns the two PDFs
order differently ("Placebo Tizanidine Tablets N = 261% N = 264%").
That last one stays critical on purpose: it contains numbers, and the
relocation rule never silently demotes a difference with digits in it.

Section matching on this pair also went from 29s to 4.5s, and the whole
comparison from 36s to 3.5s: the same few dozen headers were being
compared as strings hundreds of thousands of times, so `_TitleSimilarity`
now memoizes each distinct title comparison (identical results).

Checked in `dev_tests/test_extraction_diff_smoke.py` TEST 22: a
document whose headers each appear as a contents line AND as a section,
with one side carrying an extra copy of the leaflet. Every contents
line must pair with a contents line, the real dosage edit must come
back as one clean "4" -> "8" row, and the missing copy must be reported
as whole sections -- nothing added, nothing falsely moved. Without the
context tie-break all three checks fail (6 crossed pairings, and the
dosage edit shredded into a deleted sentence plus an added one).

## 32. A paragraph is compared with its own counterpart, not with any paragraph that shares its boilerplate

Reported from paragraph-wise proofing: a paragraph was being compared
against every paragraph in the other document, so when several
paragraphs ended with the same few lines -- storage conditions, a
"keep out of reach of children" warning, a repeated footer -- a
paragraph could be claimed by one of those look-alikes. Its real
counterpart was then left to pair with whatever remained, and both
paragraphs came back reading as completely rewritten.

The pairing (`_match_sections`) scored every chunk against every other
chunk and took the best score, with no notion of where in the document
either one sat. Nothing stopped a paragraph 40 pages away from winning
on a fractionally higher similarity.

Three changes, in the order they now apply:

1. **A 100% match ends the question** (`_identical_pairs`). A chunk
   whose text is identical to one in the other document -- and which
   occurs exactly once on each side, so there is nothing to choose
   between -- is paired immediately and taken out of the search
   entirely. It is never scored against anything else. This is also
   where most of the time goes on a long document: every anchored pair
   removes a whole row and column from the similarity search.

   Repeated identical text is deliberately NOT anchored this way.
   Boilerplate that appears twenty times is identical to every copy of
   itself, so "identical" cannot say which copy belongs to which --
   and the copies that happen to be identical in one document are
   often not the same ones in the other. Those are left to step 3.

2. **Document order comes next.** Those anchors, reduced to the subset
   that reads in order in both documents (`_monotone_backbone`, the
   classic patience-diff anchor set, so one relocated copy cannot drag
   the order off course), bracket every remaining chunk: a chunk
   between two anchors should pair with something between their
   partners. A pair that does not fit has to beat one that does by
   `_OUT_OF_SEQUENCE_PENALTY` (0.15) to win, so a look-alike elsewhere
   in the document no longer wins on a hair's breadth -- while a
   section that genuinely moved, which is a far better match than
   anything in its place, still wins and is still reported as moved.

3. **Then the best percentage**, as before: header number and title,
   then body similarity, with the surrounding text
   (`_ContextSimilarity`) and the n-th occurrence breaking ties. The
   final tie-break is now document order -- of two copies that are
   otherwise indistinguishable, the earlier one wins, where the list
   previously handed out the last.

### Verified

On a real pair where the same paragraph is repeated on every page
(`sample 5 search parawsie`), the approved paragraphs used to pair with
the LAST copy in the suspect, on page 8; they now pair with the copy on
page 1, in reading order, so the viewers land where the reviewer
expects.

Across the real pairs on hand -- the 100-page prescribing information,
the pixel leaflet, the medley report, the Rusan foil, the Matrix pair,
the superscript pair, `_d4`/`_d5` and the sample 2 DOCX/PDF -- the
reported differences are unchanged, and the 100-page comparison got
faster (0.95s to 0.68s; section matching alone 4.5s to 0.5s).

`dev_tests/test_extraction_diff_smoke.py` TEST 23 is the regression
test: two packaging paragraphs ending in the same three lines of
storage boilerplate, where the suspect rewrote the first one to
describe what the second one describes (0.97 similar to the other
paragraph, 0.89 to its own original) and reworded the second, so
neither side has an exact copy to fall back on. With the previous
pairing the two paragraphs swap partners, producing the nonsense row
"Tablet presentation." -> "Oral suspension pack." and two invented
"moved" rows, while the actual rewrite is never reported. Each
paragraph must now pair with its own counterpart, in order.

## 33. Rematch one discrepancy against the paragraph it belongs to

The comparison matches the two documents as wholes: every section and
paragraph competes with every other for a partner, and one chunk that
wins a partner it should not have leaves the rightful owner to pair
with whatever is left. The result is a difference that is not one --
two paragraphs reported as rewritten when the real story is that their
partners got swapped.

Rematch is the reviewer's answer for the one difference under the
cursor:

> **Double-click a highlighted difference in either viewer** (or
> Review > Rematch Selected, Ctrl+Shift+R).

`core/rematch.py` works on exactly the structure the comparison works
on -- the same sections and paragraphs, the same pairing, the same
reading order -- and changes one thing only: this chunk no longer
competes with the rest of the document for its partner.

1. **Find the section or paragraph the clicked text belongs to**
   (`_split_sections`, the chunking the comparison used).
2. **Work out where its partner has to be**: between the partners of
   the nearest paired chunks before and after it. Reading order is the
   constraint, so a paragraph can never be rematched to one 40 pages
   away.
3. **Pick the best partner in that range on its own merits**, with no
   other chunk competing for it -- which is how a partner taken by a
   look-alike comes back. The partner it already has is reconsidered
   too, but only wins if it is far better (`_POSITION_MARGIN`) than
   anything in the right place.
4. **Align those two paragraphs and compare only the clicked text**
   against whatever stands at the SAME PLACE in the partner
   (`_corresponding_range`). The same words elsewhere in the paragraph
   are not the counterpart; what is at this position is. The rest of
   the paragraph already has rows of its own and is left alone.

Which side was double-clicked decides which paragraph is rematched, so
"where does my Original paragraph really belong?" and "where does this
Suspect paragraph come from?" are separate questions.

| what the structure says | what the row becomes |
|---|---|
| the text matches its counterpart after all | the row is removed, with an explanation |
| it differs from its counterpart | the REAL differences, just for this text |
| nothing in that part of the document fits | the row stands, now confirmed |

**Ctrl+Shift+Z undoes the last rematch** (not Ctrl+Z, which belongs to
the toolbar's text fields), so trying one costs nothing.

### Guards

* **Position beats text.** A paragraph repeated word for word later in
  the document is a perfect match on text and the wrong answer on
  position. Tested: when the Suspect repeats a dosage paragraph
  verbatim at the end while the copy in its own place reads "8 mg", the
  rematch reports "4 -> 8" rather than declaring the paragraph
  unchanged.
* **A row of one or two words is refused**, with an explanation. Every
  document contains a stray "4", and matching one would turn a dosage
  change into "no difference". Four words minimum.
* **A chunk must be 30% alike** to be accepted as the counterpart;
  below that nothing in that part of the document fits and the row
  stands.

### Cost

The structure (chunking + pairing) is built once per comparison --
0.02s on a leaflet, 0.74s on the 100-page pair -- and reused, so the
first double-click after a comparison pays for it and the rest are
2-53ms.

Measured over every row of three real pairs: on the pixel leaflet, 9 of
60 rows change and 26 are confirmed as they stand; on the 100-page
pair, which the comparison already gets right, 0 of 34 change; on the
medley report, 7 of 17. (Rows of one or two words are refused: 25, 32
and 7 of them respectively.)

Checked in `dev_tests/test_extraction_diff_smoke.py` TEST 24 (a false
difference its own paragraph disproves, an edited paragraph reporting
just its edit, position winning over an identical copy elsewhere, a
genuine deletion confirmed, and a one-word row refused) and
`dev_tests/manual_e2e_compare_check.py` step 22 (a real double-click on
a page reaching the window, a genuine deletion left untouched, and
replace/undo of a row).

## 34. "Moved" must never swallow a real difference

Reported by the reviewer, and the reason the previous section's
default was wrong: a long paragraph gets reported in the Moved
category although it is present in both documents -- and the actual
deviations inside it are then never reviewed, because the paragraph
has already been "accounted for". Concise PDFs move almost every
paragraph, so this is not a corner case. Worse, two completely
different paragraphs that share a few lines were being called moved as
well.

### What was actually happening

`_downgrade_relocated_runs` demotes a difference to "moved" when the
text it covers exists in the other document -- the rule that stops a
multi-column leaflet from reporting a relocated block as deleted and
added. One of its tests asks only whether each WORD occurs in the other
document often enough. On a leaflet, where the same words recur
everywhere, that is nearly always true, so it was demoting real
findings:

| what it said | what it really was |
|---|---|
| moved: "Hold the" -> "Fever - Itching one pre-filled" | two unrelated passages: a critical difference |
| moved: "hand between two fingers" -> "needle or as a multipack" | the same |
| moved: "CHILDREN Rx Only POM" -> "CHILDREN." | a critical difference |

All three sat in the lowest tier, and the first two were the ONLY
mention of their text anywhere in the results -- so unticking "Moved"
removed them from the review entirely.

### The fix

A replacement is only a relocation when its two sides are
recognizably **the same text** (`_is_same_text_relocated`: at least
`_RELOCATION_SAME_TEXT_RATIO` = 0.5 alike, with single words left to
the existing count test). Two passages that merely share a few words
are a real difference, whatever else the documents contain.

Measured across eight real pairs, exactly three rows change -- the
three above, each from "moved" back to "critical". Row counts, and
every other row, are untouched:

| pair | before | after |
|---|---|---|
| pixel leaflet | 21 critical, 28 moved | **23 critical**, 26 moved |
| Matrix | 2 critical, 2 moved | **3 critical**, 1 moved |
| 100 pages, medley report, d4/d5, rusan foil, supsub, sample 2 | unchanged | unchanged |

Checked across those pairs afterwards: **no moved row hides a
difference that is reported nowhere else.**

### And "Moved" is listed again by default

Section 33 hid the category by default. That was wrong for exactly the
reason above: a moved row can be the only place a passage is mentioned,
so hiding it takes that passage off the review. Every category is
listed again; one click still hides it, and "Ignore minor and below"
still covers it.

What the category means now, and what it does NOT mean:

* it means the same text is in both documents, in a different place;
* it never means the wording inside it went unchecked. A paragraph
  that moved AND changed reports the change as its own row, at its own
  severity -- verified in TEST 25 with a dosage edit inside a moved
  paragraph, which comes back as a critical "4 -> 8" row.

Checked in `dev_tests/test_extraction_diff_smoke.py` TEST 25: unrelated
passages are not a relocation, a near-identical passage still is, a
difference between unrelated passages survives the relocation pass, and
a dosage change inside a moved paragraph is still reported.

## 35. Ruling lines drawn over a paragraph are not a table

Section 34 ended on an honest loose end: a few differences on the
medley report read as "moved" because the APPROVED file's own text came
out scrambled --

    ... This includes any the Yellow Play or possible Card Apple App
    side Scheme Store. ...

Three consecutive lines of one paragraph, read in vertical strips.

### Cause

`find_tables` found eight "tables" on that page. One of them, over the
"Reporting of side effects" paragraph, was a grid inferred from the
proof sheet's box art -- rules drawn across flowing text. Its columns
cut the lines into pieces ("Sche", "Apple Ap", "p Stor"), and a table
is read cell by cell, so the paragraph's three lines came back
interleaved strip by strip.

`_looks_like_real_table` already rejected decorative grids whose rows
populate only one column; this grid passes that test, because a line of
flowing text sliced into five pieces does populate five columns.

### The fix

A real table's column edges run BETWEEN its columns, so no word ever
crosses one. A grid inferred from ruling lines over flowing text has
edges running straight THROUGH words. `_cuts_through_words` rejects a
grid when more than `_MAX_WORD_STRADDLE_FRACTION` (15%) of the words
inside it straddle one of its column edges.

Measured on the real documents, the two cases do not overlap:

| | words straddling a column edge |
|---|---|
| genuine tables (sample 2's dosage tables, the medley report's own ruled tables on pages 1 and 9) | 0-5% |
| the inferred grids on the scrambled page | 18%, 20%, 32%, 50%, 61%, 100% |

### Result

That page now reads as written: "... This includes any possible side
effects not listed in this leaflet. You can also report side effects
directly via the Yellow Card Scheme at: www.mhra.gov.uk/yellowcard or
search for MHRA Yellow card in the Google Play or Apple App Store ..."

And the comparison of that pair stops reporting the scramble:

| | before | after |
|---|---|---|
| rows | 17 | **11** |
| words flagged | 3,373 | **39** |
| "moved" rows | 7 | **0** |

What remains is real: five pieces of artwork text the suspect adds
("120 mm FRONT", "SAME SIZE ARTWORK, SIZE : 120 x 725 mm ..."), one
"information" -> "informationent." and five hyphenation differences
("Cocodamol." -> "Co- codamol.").

Every other real pair -- the 100-page prescribing information, the
pixel leaflet, the Rusan foil, Matrix, the superscript pair, `_d4`/`_d5`
and sample 2 DOCX/PDF -- comes back byte for byte unchanged.

Checked in `dev_tests/test_extraction_diff_smoke.py` TEST 26: a PDF
whose three-line paragraph is crossed by three vertical rules must
still read line by line (without the fix it reads "This includes any
leaflet. You can Yellow Card Scheme today. possible side effects ..."),
a grid whose edges run through words is rejected, and a grid whose
edges run between its columns is kept.

## 36. Letter-by-letter comparison

The comparator marked which words differ. For a flagged string a
proofer needs the next question answered too: exactly WHICH character,
and what kind of change is it? The pane now shows that, under
**Letter by letter** (the "Letters" tick box in its header):

    ORG  d d / m m / y y
    SUS  0 6 / 0 1 / 2 5

    'd' U+0064 LATIN SMALL LETTER D  ->  '0' U+0030 DIGIT ZERO  -- letter changed to a digit
    'd' U+0064 LATIN SMALL LETTER D  ->  '6' U+0036 DIGIT SIX   -- letter changed to a digit
    'm' U+006D LATIN SMALL LETTER M  ->  '0' U+0030 DIGIT ZERO  -- letter changed to a digit

The two texts are lined up character under character
(`core.char_compare.align_characters`), so the differences sit in the
same columns and a character present on one side only leaves its column
empty. Every differing character is then named by its code point and
its Unicode name, and the change is CLASSIFIED -- because the
classification is usually the finding:

| what the characters are | what it is called |
|---|---|
| 'E' U+0045 -> 'e' U+0065 | upper to lower case |
| '2' -> '3' | digit changed |
| 'd' -> '0' | letter changed to a digit |
| 'e' U+00E9 -> 'e' U+0065 | accent changed |
| 'a' U+0061 -> 'a' U+0430 | look-alike letter: cyrillic instead of latin |
| space U+0020 -> no-break space U+00A0 | space changed to no-break space |

The look-alike row is the one a reviewer cannot catch by reading:
"paracetamol" with a Cyrillic "a" is identical on the page and a
different string in the file. Invisible characters are named rather
than printed blank, so a non-breaking space that became an ordinary one
-- which will reflow the line -- is visible as itself.

Long strings are not lined up in full (over `_MAX_ALIGNED_CHARS` = 90
characters the alignment is unreadable anyway); the characters that
differ are still listed, the first `_MAX_LISTED_CHARS` = 6 of them with
a count of the rest. A difference that exists on one side only says so
instead of pretending to align.

`core/char_compare.py` is pure text in, text out -- no Qt, no colors --
so the classification is testable on its own.

Checked in `dev_tests/test_extraction_diff_smoke.py` TEST 27 (alignment
including a character present on one side only, naming by code point
and Unicode name, invisible characters named, each kind of change
classified correctly, the Cyrillic look-alike caught, and the summary
leading with the most serious kind) and
`dev_tests/manual_e2e_compare_check.py` step 23 (the pane showing the
code points for a real date change, the look-alike case, a one-sided
difference, and the tick box switching the view off).

## 37. The window fits the screen, and differences appear as they are found

Two things the reviewer could not see.

### The bottom of the window was off the screen

Three separate causes, all fixed:

* **The window could be taller than the desktop.** A geometry saved on
  a bigger monitor, or the 1440x960 default on a smaller one, left the
  bottom of the window under the taskbar -- which is where the
  Deviations dock lives. `MainWindow._fit_to_screen` now clamps the
  window to the usable area of its screen, at startup and after
  restoring a saved geometry.
* **The window could not be made narrow.** The deviation list's filter
  chips and view options were one long unwrapping row each, so the
  panel's minimum width was about 2,000px and the window could not be
  shrunk below it. Both rows now wrap (`ui/flow_layout.py`), and the
  panel's minimum width is **328px** instead of 2,015. Each row sits in
  a `FlowRow`, which keeps itself exactly as tall as its wrapped lines
  -- as a bare sub-layout it was handed the spare vertical space, 64px
  per row, straight out of the comparator below it.
* **The comparator was taller than the dock.** With the
  letter-by-letter block added it needed more room than the dock gave
  it, so the bottom was simply cut off. Its content now sits in a
  scroll area, its text views ask for their minimum height rather than
  QTextBrowser's 190px default, the letters block shares the pane's
  height with the two texts, and the dock opens at 420px.

Together: the comparator went from 234px of a 420px dock to 307px, and
the whole stack -- both texts and the letter-by-letter block -- is on
screen without scrolling.

### Differences now appear while the comparison runs

The list used to fill in one jump when the whole comparison finished.
`diff_documents(..., on_found=...)` now hands out each section's
differences as that section is compared; `DiffWorker` batches them on a
120ms timer (a signal per difference costs more than the comparison
does) and `ResultsPanel.append_results` adds them, with the status bar
counting up.

Two things make this honest rather than just busy:

* **What is shown is what remains.** The passes that run once the whole
  document has been compared can still merge or re-rank rows, so the
  batches are provisional and `set_results` replaces them at the end.
  To stop rows appearing and then being taken away, the spacing
  artifacts that the closing pass drops are left out of the stream too
  (`_is_whitespace_artifact`). On the 100-page pair that took the
  stream from 135 rows settling to 34 -- a visible collapse -- down to
  35 settling to 34.
* **The results do not depend on anyone listening.** With and without a
  listener, `diff_documents` returns the same list.

Honest about the gain: on the pairs here the rows take 0.1-0.2s to
produce (the time before them is splitting and pairing, 0.12-0.59s),
so this buys a tenth of a second or two on a comparison and is mostly
about the list filling rather than jumping. The wait you actually feel
on a long document is extraction, which runs in the background from the
moment a file is opened (section 21).

Checked in `dev_tests/test_extraction_diff_smoke.py` TEST 28
(differences arriving in several batches, the streamed set matching the
final set, spacing artifacts left out of both, and the result being the
same with no listener).

## 38. The deviation list stopped stuttering

Section 37 made differences appear as they are found, and the list
stuttered the whole way through. Two causes, both measured.

### Every batch rebuilt the whole table

`append_results` called `_refresh_table`, which clears the table and
fills every row again. Arriving in batches, that is the first fill
repeated once per batch -- quadratic work for no reason:

| differences | streaming, before | after |
|---|---|---|
| 100 (4 batches) | 45 ms | **22 ms** |
| 500 (20 batches) | 411 ms | **104 ms** |
| 1500 (60 batches) | **2,611 ms** | **320 ms** |

The new rows are now appended to the table instead (`setRowCount` plus
`_fill_row` for the new ones only), so the cost per batch is flat at
about 5ms however long the list already is, rather than climbing to
43ms. Grouping identical rows still rebuilds, because a new difference
can join an existing group.

The highlights on the documents are no longer redrawn per batch
either: `append_results` does not emit `view_changed`, so the hundreds
of overlay items are drawn once when the comparison finishes, as they
were before.

### Moving through the list rebuilt the comparator every step

Each selection change rebuilds the comparator pane -- two texts and,
since section 36, the letter-by-letter alignment -- which costs about
10ms of HTML. Holding an arrow key or Next-difference walks rows faster
than that, so the pane fell behind the selection.

A change coming from the table is now coalesced on a 30ms timer: only
the row actually landed on is drawn. A difference clicked in a viewer
(`select_item`) still updates immediately.

### Where it stands

Measured on a list of 1,500 differences:

| action | cost |
|---|---|
| a batch of new differences arriving | ~6 ms |
| scrolling (per step) | 3 ms |
| selecting a row | 9 ms |
| filtering by text | 42 ms |
| toggling a category chip | 84 ms |
| accepting the selection | 113 ms |

The last two are full rebuilds of the table. At the few hundred rows a
normal document produces they are 30ms; at 1,500 they are a visible
hitch, and would be the next thing to make incremental if it matters.

## 39. Nothing a reviewer must see is filed as minor (MVP-38)

A risk audit (`AUDIT.md`) reproduced the ways a real deviation could be
hidden -- rated `minor`/`typo`/`moved`, sorted to the bottom, and removed
from view by "Ignore minor and below" -- or never compared at all. All
of them are fixed; each fix is locked in by a test that fails on the old
code (smoke TESTs 29-32, GUI step 24).

### Severity

* **The chart-artwork rule no longer outranks a changed number.** It ran
  before the digit rule and joined the two sides with `or`, so one short
  artwork-looking side (`4:26 pm, Sep 05, 2023`) demoted a whole missing
  document body to `minor`. The digit rule now runs first; a replacement
  is figure text only when BOTH sides are; and text carrying label
  content -- a dose (`20 mg`), an instruction or negation, storage,
  batch or expiry -- is never demoted (`_carries_label_content`).
* **The rule itself is narrower.** Chart vocabulary is matched as whole
  words and needs a word only charts use (`Cmax`, `AUC`, `PK`, `CI`,
  `fold`...); units no longer count towards an "axis"; atom symbols
  must be written as a structure diagram writes them, so `no`, `of`,
  `off` are words again; device callouts must be part names. 11 of 22
  ordinary label lines used to qualify; none do now, and every real
  chart sample in `test_sample2_layout_diff` still does.
* **`typo` is the shape of text-layer noise only:** one side is the
  other with letters LOST, each lost run a doubled letter
  (`il ness`) or a ligature glyph (`speci cally`, `rst`). A letter added
  or replaced is a real misspelling, now `major` (category Spelling).
  On the real test pairs the planted errors (`contrainsdicated`,
  `ЛОРАТАЖИН`, `MATRX`) were all hidden as `typo`.
* **Meaning changes are critical:** a negation added or removed
  (`should`/`shouldn't`, `compatible`/`incompatible`) or a prefix
  swapped for its opposite (`hypertension`/`hypotension`), and anything
  with digits or a slash (`mg/kg` -> `mg/g`).
* **A decimal separator change is a number change** (`5.000` vs
  `5,000`), compared as the separator between digit groups so list
  spacing (`2.3, 5.1` vs `2.3,5.1`) is not.
* **Spacing next to a digit is reported** (`100 mg` vs `100mg`, as
  minor spacing) instead of being dropped; a word drawn in pieces
  (`spe cifically`) is still dropped.

### "Moved"

* A deletion or insertion is only "moved" when the passage is in the
  other document **verbatim** (or as a few verbatim fragments, for
  column interleaving) -- never because its words occur somewhere. A
  deleted `Patients should not take this medicine with alcohol.` was
  "moved"; on the Matrix pair an added `S Rx Only POM` (0 copies in the
  original, 2 in the suspect) was too.
* A replacement is moved only when **both** its sides are found elsewhere.
* A moved row now shows **both positions** instead of an empty side.

### Extraction

* **Word boundaries on small type.** The fast glyph source drops most
  space characters (304 of 5,138 on one 6.3pt page), and a 2pt gap
  tolerance swallows a 1.5pt word space, so whole lines came out as one
  token (`patientswhohavenothealedafter4to8weeksoftreatment,`). A
  font-relative tolerance splits justified words into letters instead,
  so MuPDF's own word boxes are used to ADD breaks only
  (`MuPdfPage.extract_words`). On that leaflet: 9,250 words instead of
  6,665; every other real pair is unchanged. Cost: +20% extraction time
  on that dense page; right-to-left words are left as they were.
* **PowerPoint:** grouped shapes (nested too) are read -- a slide built
  from grouped boxes extracted nothing -- and so are speaker notes.
* **Word (approximate layout, no Word/LibreOffice):** headers, footers,
  footnotes, endnotes and text boxes are read.
* **Excel:** a formula cell with no cached value is compared by its
  formula instead of disappearing.

### What could not be compared (`core/coverage_check.py`)

A red banner above the list -- and the report -- names pages with no
extractable text, words the PDF's font cannot decode, and a side that
yielded a fraction of the other's words. The last also opens a dialog:
a suspect that extracted 9 words against 6,653 was reported as
"2 differences". Blank pages alone only get the banner, so the dialog
stays rare enough to be read.

### The review is kept and can be exported

* Decisions and "viewed" marks given while rows were still streaming
  in are no longer wiped when the comparison finishes.
* The review is saved as it changes (`core/review_store.py`), keyed by
  the SHA-256 of both files, and restored when the same two files are
  compared again. A changed file is a different comparison.
* **File > Export Report... (Ctrl+E)** writes HTML or CSV: both files'
  names, sizes and SHA-256, the build, the reviewer, what could not be
  compared, every difference with severity, category, pages, decision,
  reviewer and time, and a log of every rematch -- including rows a
  rematch removed, which otherwise left no trace.
* The list can be sorted by **page order** as well as severity.
* The "Ignore minor and below" tooltip now says what it really hides.

### The test suites

The smoke suite had 7 long-standing failures; it now passes in full.
Four were stale expectations rather than defects -- a move reported
once with both positions instead of as two half-rows (2), and a
two-column fixture whose one-token "columns" were really a label/value
table, read row-major by design. The colon-header case was a genuine
contradiction between two tests; the code's behaviour (no section break
at `Manufactured by:` in a numbered document) is kept, and the smoke
test now checks what matters -- that a manufacturing-site change inside
such a block is still critical when the block also moves.

## 40. Faster extraction, identical output

Extraction CPU over the 19 real PDFs in the regression corpus went from
**153.8s to 58.5s** (2.6x), and every extracted word is bit-for-bit the
same -- text, page and full-precision coordinates -- as are the
comparison results on all 10 real pairs. Through the app's own parallel
path (7 worker processes, both documents at once):

| pair | before | after |
|---|---|---|
| medley report | 9.6-11.4s | **4.1-4.7s** |
| ESMOLOC leaflet (rotated, 6.3pt) | 20.1-23.4s | **9.7-10.6s** |
| 100-page | 5.7-5.9s | 5.4-5.7s |

("Before" undoes only the two largest changes, so it understates the
old times.) The 100-page pair is limited by the machine's spare cores,
not by extraction work.

What changed, and why each is exact:

* **Table geometry** (`core/fast_tables.py`, installed over
  pdfplumber's): `edges_to_intersections` sorted every horizontal edge
  once per vertical edge; `intersections_to_cells` rebuilt sets of edge
  boxes on every comparison and rescanned all remaining points for each
  point. Now: one sort and a bisected slice (pdfplumber's own test then
  applied verbatim), sets built once per point, points grouped by row
  and column. Same intersections, same edge objects in the same order,
  same cells -- checked on 302 real pages and 1,200 random grids. 12.1s
  -> 0.9s on the corpus.
* **Vector paths** are read with `get_cdrawings()` (plain tuples)
  instead of `get_drawings()` (a Python Point per coordinate -- 3.3
  million on one outlined scan), and only segments a consumer can use
  are kept: the table finder takes horizontal/vertical edges only, the
  gutter-rule check near-horizontal lines of 30pt+. A page with no
  upright words skips table detection altogether. The outlined scan:
  43s -> 8.5s CPU.
* **Rotated pages**: each glyph used to be rotated through three
  PyMuPDF objects (~60us). The span is now transformed in one numpy
  step in MuPDF's own single precision -- bit-identical on 512,283 real
  and random transforms. Glyph reading on the ESMOLOC leaflet: 8.5s ->
  1.4s. (numpy is now a declared dependency.)
* **Superscript/subscript detection**: the "largest glyph nearby" is
  cached per line and bucket instead of recomputed for each of ~2
  million glyphs, and the neighbour index is built only on a page that
  has a candidate (387 of 103,818 glyphs on the 100-page PDF).
* **Small things**: a fast path for the ~99% of glyphs that are not
  ligatures (written with the general formula's exact float
  expressions); MuPDF word boxes kept as tuples and indexed on a grid;
  the margin page-number filter finds a word's line-mates by bisection
  instead of scanning every word on the page.

Left alone: what remains on an outlined scan is MuPDF's own C
interpretation of a million paths, and skipping table detection on
pages with only a few words could change results.

Checked in smoke TEST 33 (fast table geometry against pdfplumber's on
random grids, rotated coordinates against PyMuPDF's, a rotated page
reading in order).
