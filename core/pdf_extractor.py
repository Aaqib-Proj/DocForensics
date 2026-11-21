"""Coordinate-aware, column-aware PDF text extraction (core/pdf_extractor.py).

Milestone scope: extraction + comparison ONLY. No Qt imports here --
this module is standalone-testable via dev_tests/.

--------------------------------------------------------------------
Why WORDS, not LINES or BLOCKS
--------------------------------------------------------------------
A "concise"/resized PDF reflows text: the same sentence wraps onto
different lines and the gaps between words stretch or shrink to fill
the new column width. Nothing about the *words themselves* or their
reading order changes.

If you diff at the level of raw lines or paragraph blocks, a line
wrap turns "hello world" into "hello\nworld" and difflib reports the
whole line as changed even though not a single word differs. The fix:
extract a flat, per-page WORD STREAM with pdfplumber, retain each
word's bounding box for click-to-navigate, and diff at word
granularity (see core/diff_engine.py). Line boundaries and whitespace
are reconstructed on the fly purely to get words in the correct
reading order -- they are then discarded before comparison, so reflow
and uneven spacing can never cause a false diff.

--------------------------------------------------------------------
MVP-5: column-aware reading order (fixes the MVP-4 known limitation)
--------------------------------------------------------------------
MVP-4 sorted every word on a page by `top` only, then left-to-right
within each visual row. That is correct for single-column documents,
but on a multi-column layout (e.g. a 3-column FDA label) it produces
garbage: words from column 1, column 2 and column 3 that happen to
sit at the same height get interleaved into one "row", so the output
reads left-to-right *across* columns instead of top-to-bottom *within*
each column before moving to the next one.

The fix below detects column bands per page BEFORE any row grouping,
then orders words column-by-column (each column top-to-bottom), and
finally concatenates the columns left-to-right. Detection works for
any number of columns (including 1, i.e. a normal single-column page)
and is driven purely by the words' own bounding boxes -- no hardcoded
column count or x-coordinates.

How column detection works (`_detect_column_bands`)
=====================================================
1. Group words into rows by vertical position only (`_group_into_lines`),
   ignoring columns entirely -- a row here is just "words that sit at
   about the same height", which on a multi-column page mixes together
   one line from each column that happens to align.
2. For every row, merge its words' x-intervals into a small set of
   non-overlapping spans (`_merge_intervals`).
3. Sweep across the page's x-axis and count, at every point, how many
   *rows* have some word covering that x -- not how many words, so a
   short header that happens to bridge a gutter only contributes to
   the count for the 1-2 rows it occupies, not for every row on the
   page.
4. Any interior x-range where that row-crossing count stays at or
   below a small tolerance (a handful of rows, e.g. an occasional
   title or table cell bridging the gap) for a wide enough span is a
   real column gutter. The regions between gutters are the column
   bands, left to right.

This is intentionally a page-level, geometry-only heuristic for the
NON-TABLE words -- it has no notion of "this is a heading". Tables are
now carved out FIRST (MVP-7 below), so a wide table's internal cell
gutters can no longer slice column bands through it.

--------------------------------------------------------------------
MVP-7: table-aware reading order + Unicode normalization
--------------------------------------------------------------------
Real medical/regulatory PDFs (drug labels, clinical-trial tables) mix
body text with wide ruled tables, and that combination produced false
positives once the MVP-5 ordering met it:

1. A table's own cell gutters could be mistaken for column gutters, so
   the page was sliced into bands cutting THROUGH the table and its
   words were read in the wrong order ("double- Dizziness 11 5 When"
   style scrambles).
2. A line-end hyphen in one table cell could be stitched onto the
   first word of the NEXT cell ("double-" + "When" -> "doubleWhen"),
   fabricating a phantom token that matches nothing on the other side.

The fix (see `_detect_page_tables` / `_order_page_words`): detect
tables first (pdfplumber find_tables, ruling lines), assign every
word to its grid cell, order each table cell-by-cell in row-major
order, and only then run the MVP-5 column-band heuristic on the
remaining body words, interleaving table rows back into their band by
vertical position. Hyphen-stitching is guarded so it can never cross a
cell boundary, and every word is Unicode-normalized once at extraction
time (NFKC, zero-width chars, soft hyphens, NBSP, ligatures) so
invisible text-layer artifacts can never cause a false diff. Tables
without visible ruling lines (pure whitespace grids) are NOT auto-
detected -- pdfplumber's word-alignment fallback was tried and
rejected because it misdetects ordinary multi-column body text as a
table; that remains a documented limitation for a future slice.

--------------------------------------------------------------------
MVP-9: RTL (Arabic/Hebrew) text handling
--------------------------------------------------------------------
RTL documents required two layers of correction:

1. WORD ORDER: lines were sorted by x0 ascending (LTR), but for
   Arabic/Hebrew the first logical word sits at the highest x0
   (rightmost). **Detected at the PAGE level** -- not per-line --
   because per-line detection is fragile: a single mixed-script line
   can flip between RTL and LTR across two renderings of the same
   content (different font metrics → different word groupings →
   different letter-count ratios), causing catastrophic stream
   misalignment.  Page-level detection is stable because a whole
   page's character composition doesn't change between renderings.

2. CHARACTER ORDER: pdfplumber's WordExtractor merges each word's
   characters ascending by x0, reversing every RTL word's letters
   (e.g. "أبديل" extracted as "ليدبأ"). Fix: detect RTL words and
   reverse their characters after extraction. (This was temporarily
   removed in a previous revision due to a misunderstanding of how
   mixed-script text behaves under x0-descending layout, but is
   strictly required so the UI displays Arabic correctly).

3. BRACKET MIRRORING: different PDF producers bake in mirrored
   bracket codepoints for RTL rendering (one file has '(' where the
   other has ')' at a parallel position). Fix: canonicalize all
   bracket-class codepoints to a single direction at normalization.

4. ARABIC LIGATURE INCONSISTENCY: NFC treats precomposed
   presentation-form ligatures (e.g. U+FEFB ﻻ) as distinct from the
   decomposed base letters (ل + ا); NFKC decomposes them. Fix:
   switched normalization from NFC to NFKC.

An additional severity-rule refinement: the "digit anywhere =>
critical" rule was amplifying nearly every RTL misalignment into
"critical" because Arabic pharmacological text is dense with digits.
The new rule only escalates when the actual digit values differ.

--------------------------------------------------------------------
MVP-14: rotated (sideways) text reconstruction
--------------------------------------------------------------------
Packaging/carton artwork routinely prints a caption sideways -- a
spine label ("Tizanidine Hydrochloride Capsules" rotated 90 degrees)
or a dimension callout ("33 mm") next to a die-line arrow. pdfplumber's
word grouping assumes horizontal text: it clusters characters into a
word using the gap between one character's RIGHT edge and the next
character's LEFT edge, which only means anything for left-to-right
horizontal glyphs. A rotated character's bounding box has nearly
IDENTICAL x0/x1 to its neighbors (the box barely moves sideways; it's
the `top` that changes as the text advances downward or upward), so
that horizontal-gap test never recognizes two rotated characters as
belonging to the same word. Every rotated character comes back from
`page.extract_words()` as its own separate one-character "word", and
the page's line/column-band ordering -- which also assumes horizontal
text -- then scatters these single-character fragments into whatever
"line" they happen to share a similar page height with, interleaving
them with unrelated body text at a similar y-position and sorting the
result by x0. The observed failure was exactly this: a real spine
caption came back as a scrambled, unreadable stream of individual
letters ("e d e se i r n o i l d l u h i s n c p o a a z r d C i T y H")
mixed in with dimension-callout digits, which both pollutes the word
stream feeding the diff and produces a garbled, meaningless "critical"
discrepancy instead of readable text.

The fix (`_reconstruct_rotated_words` / `_ordered_rotated_words`):
every character carries its own PDF text matrix (a, b, c, d, e, f),
which maps the character's local +x axis (the direction consecutive
characters of a word advance along) to a page-space direction vector
(a, b), and its local +y axis (how one line offsets from the next) to
(c, d). Projecting a character's page position onto these two vectors
gives an "advance" coordinate that increases along that character's OWN
true reading direction, and a "perpendicular" coordinate that is
constant within one line of that rotated text and changes between
stacked lines -- i.e. the exact rotated-frame equivalent of the (x0,
top) pdfplumber would use for ordinary horizontal text (and reduces to
plain (x0, top) sorting when a character isn't rotated at all, a=1,
b=0, c=0, d=1). This groups characters into correctly-ordered words and
lines independent of pdfplumber's horizontal-only assumption, for any
rotation a PDF text matrix can express (the common 90/180/270-degree
cases, and skewed angles too) -- verified against the reported file's
real spine caption and dimension callouts, both of which reconstruct
perfectly ("Tizanidine", "Hydrochloride", "Capsules", "33", "mm" as
correct standalone words, not "e d e se i r..." noise) instead of
relying on pdfplumber's undocumented and, empirically, unreliable
behavior on rotated text.

Reconstructed rotated text is then carved out as its own self-contained
reading-order block (like a table's cells) rather than re-interleaved
character-by-character with body text: it is typically decorative page
furniture (a spine caption, a dimension callout) rather than part of
the grammatical body flow, so getting its exact position relative to
surrounding paragraphs right matters far less than (a) never being
scrambled and (b) landing in a stable, comparable stream position
across two renderings of the same artwork -- both guaranteed by
appending each page's rotated blocks, in a deterministic left-to-right
order, after that page's regular body/table reading order.
"""

from __future__ import annotations

import bisect
import logging
import math
import os
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

import fitz  # PyMuPDF -- PDF text extraction and page rasterization for multi-format rendering
import pdfplumber

from core import fast_tables
from core.mupdf_page import MuPdfPage
from core.script_text import SCRIPT_CHARS, SUBSCRIPT, SUPERSCRIPT, join_runs
from core.section_diff import is_likely_header_line

# pdfplumber's table geometry, minus its quadratic work -- identical
# results (see core/fast_tables.py).
fast_tables.install()

logger = logging.getLogger(__name__)


# --- Faster character objects ----------------------------------------------
# pdfplumber converts every pdfminer object to a dict by running a
# recursive `resolve_all` over each attribute. For characters -- by far
# the most numerous objects, and all plain values -- that generic pass
# was the single largest cost of extraction. This builds the identical
# dict for LTChar directly (same keys, same values, same coordinate
# adjustments as pdfplumber.page.Page.process_object) and leaves every
# other object type to pdfplumber.
from pdfminer.layout import LTChar  # noqa: E402
from pdfplumber import page as _plumber_page  # noqa: E402
from pdfplumber.utils.pdfinternals import resolve_and_decode as _resolve_and_decode  # noqa: E402

_CHAR_ATTRS = ("adv", "fontname", "height", "matrix", "mcid", "size", "tag", "upright", "width", "x0", "x1", "y0", "y1")
_plumber_process_object = _plumber_page.Page.process_object


def _process_object_fast(self, obj):
    if type(obj) is not LTChar:
        return _plumber_process_object(self, obj)
    values = obj.__dict__
    attr = {key: values[key] for key in _CHAR_ATTRS if key in values}
    attr["object_type"] = "char"
    attr["page_number"] = self.page_number
    if "ncs" in values:
        attr["ncs"] = _resolve_and_decode(values["ncs"].name)
    text = obj.get_text()
    unicode_norm = self.pdf.unicode_norm
    attr["text"] = _plumber_page.normalize_unicode(unicode_norm, text) if unicode_norm is not None else text
    gs = obj.graphicstate
    attr["stroking_color"] = gs.scolor if isinstance(gs.scolor, tuple) else (gs.scolor,)
    attr["non_stroking_color"] = gs.ncolor if isinstance(gs.ncolor, tuple) else (gs.ncolor,)
    if isinstance(attr["fontname"], bytes):
        attr["fontname"] = _plumber_page.fix_fontname_bytes(attr["fontname"])
    mb_x0, mb_top = self.mediabox[:2]
    attr["top"] = (self.height - attr["y1"]) + mb_top
    attr["bottom"] = (self.height - attr["y0"]) + mb_top
    attr["doctop"] = self.initial_doctop + attr["top"]
    if mb_x0 != 0:
        attr["x0"] = attr["x0"] + mb_x0
        attr["x1"] = attr["x1"] + mb_x0
    return attr


_plumber_page.Page.process_object = _process_object_fast


# Page source for extraction: "mupdf" (default) reads characters and
# drawings with PyMuPDF -- see core/mupdf_page.py -- and "pdfplumber"
# the original pure-Python way (kept for comparison and as a fallback).
PDF_ENGINE = os.environ.get("DOCFORENSICS_PDF_ENGINE", "mupdf")


@contextmanager
def _open_pdf_pages(pdf_path: str):
    """(page count, page-by-index getter) for the configured engine."""
    if PDF_ENGINE == "pdfplumber":
        with pdfplumber.open(pdf_path) as pdf:
            yield len(pdf.pages), lambda index: pdf.pages[index]
        return
    doc = fitz.open(pdf_path)
    try:
        yield doc.page_count, lambda index: MuPdfPage(doc[index])
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# RTL (Right-to-Left) script detection  (page-level)
# ---------------------------------------------------------------------------
# Arabic/Hebrew text is laid out right-to-left on the page, so the
# *first* logical word sits at the highest x0 (rightmost).  The sort-
# by-x0 that gives correct LTR reading order reads every RTL line
# backwards.
#
# RTL detection is done at the PAGE level, NOT per-line.  A page-level
# decision is far more stable across two different renderings of the
# same content: a single line mixing Arabic and Latin text can have its
# character counts flip between two PDFs (slightly different glyphs or
# font metrics change which characters pdfplumber groups into which
# word), causing per-line detection to sort one document's line LTR and
# the matching line in the other document RTL -- a catastrophic
# misalignment that cascades into hundreds of false diffs.  A full
# page's character composition is essentially invariant across
# renderings.
#
# Character order WITHIN individual words MUST be reversed for Arabic.
# pdfplumber's WordExtractor merges characters ascending by x0, which
# correctly preserves LTR text but forces Arabic letters into reverse
# order (e.g. "أبديل" becomes "ليدبأ"). We use `_text_is_rtl` to detect
# majority-RTL words and apply `[::-1]` to correct their display.
# This prevents the UI from showing backwards Arabic text.

# Unicode bidi categories that are inherently right-to-left.
_RTL_BIDI_TYPES = frozenset({"R", "AL", "AN"})

# Bidi categories treated as an embedded LEFT-to-right run for MVP-15's
# per-character reordering below: Latin letters and Western digits.
# Arabic-indic digits (bidi "AN") are deliberately NOT included here --
# they're already covered by _RTL_BIDI_TYPES and read the same
# direction as the surrounding Arabic text.
_LTR_BIDI_TYPES = frozenset({"L", "EN"})


def _text_is_rtl(text: str) -> bool:
    """True if the majority of *letter* characters in ``text`` are RTL.

    Digits, punctuation, and whitespace are direction-neutral; only
    actual letters/marks vote.  An empty or all-neutral string returns
    False (treated as LTR by default).
    """
    rtl = 0
    ltr = 0
    for ch in text:
        bidi = unicodedata.bidirectional(ch)
        if bidi in _RTL_BIDI_TYPES:
            rtl += 1
        elif bidi == "L":
            ltr += 1
    return rtl > ltr


def _char_bidi_class(ch: str) -> str:
    """'rtl' / 'ltr' / 'neutral' classification for one character --
    simplified for word-internal reordering (no embedding levels, no
    explicit directional marks; just enough to stop an embedded
    Latin/digit run from being corrupted, see `_fix_rtl_word_chars`)."""
    bidi = unicodedata.bidirectional(ch)
    if bidi in _RTL_BIDI_TYPES:
        return "rtl"
    if bidi in _LTR_BIDI_TYPES:
        return "ltr"
    return "neutral"


def _fix_rtl_word_chars(text: str) -> str:
    """Reorder an RTL word's characters into logical reading order.

    pdfplumber's WordExtractor merges a word's characters ascending by
    x0, which is correct for LTR scripts but stores an RTL run's glyphs
    in VISUAL order -- the reverse of logical reading order (e.g.
    Arabic "أبديل" extracted as "ليدبأ"). A naive whole-string reversal
    (this function's previous implementation, `return text[::-1]`)
    fixes a pure-RTL word, but pdfplumber routinely merges a batch
    code, PIN code, or Latin acronym directly onto an adjacent Arabic
    word with no space between them (e.g. a real observed case:
    ",<arabic letters>,MIDC" as ONE word) -- reversing THAT whole
    string corrupts the embedded run too: "210" would become "012",
    and "MIDC" was observed becoming "CDIM" (a genuine acronym
    corrupted into a different string entirely).

    This instead applies a simplified Unicode Bidi Algorithm: each
    character is classified rtl/ltr/neutral (`_char_bidi_class`);
    neutral characters (punctuation, connectors) resolve to whichever
    direction flanks them on both sides (rule N1), defaulting to this
    word's own RTL direction when flanked by different directions or
    at either end (rule N2 default, appropriate here since
    `_text_is_rtl` already decided the word is RTL overall). The
    resulting direction runs are then reversed as a SEQUENCE (the word
    reads right-to-left overall) while each RTL run's own characters
    reverse and each LTR run's characters keep their original order --
    exactly the treatment a real bidi renderer gives an embedded
    number/Latin run inside RTL text. Verified against the reported
    real cases: "210-<arabic>" (hyphen attached to neither run cleanly)
    now correctly becomes "<arabic>-210", and the "MIDC" acronym case
    above becomes "MIDC,<arabic>," with the acronym intact.

    ONLY call this on words that have been identified as RTL overall
    (`_text_is_rtl`) -- a word with no RTL characters at all has
    nothing to reorder.
    """
    classes = [_char_bidi_class(ch) for ch in text]
    resolved = _resolve_neutral_runs(classes)

    runs: list[tuple[str, list[str]]] = []
    for ch, cls in zip(text, resolved):
        if runs and runs[-1][0] == cls:
            runs[-1][1].append(ch)
        else:
            runs.append((cls, [ch]))
    runs.reverse()
    return "".join(
        "".join(reversed(chars)) if cls == "rtl" else "".join(chars) for cls, chars in runs
    )


def _resolve_neutral_runs(classes: list[str]) -> list[str]:
    """Resolve each maximal run of "neutral" entries to a concrete
    direction, per simplified Unicode Bidi Algorithm rules N1/N2: a
    neutral run flanked by the SAME direction on both sides takes that
    direction; flanked by different directions, or at either end of
    the sequence, takes the overall (RTL) direction -- see
    `_fix_rtl_word_chars`, the only caller."""
    resolved = list(classes)
    n = len(resolved)
    i = 0
    while i < n:
        if resolved[i] != "neutral":
            i += 1
            continue
        j = i
        while j < n and resolved[j] == "neutral":
            j += 1
        left = resolved[i - 1] if i > 0 else None
        right = resolved[j] if j < n else None
        if left is not None and left == right:
            fill = left
        elif left is None and right is not None:
            fill = right
        elif right is None and left is not None:
            fill = left
        else:
            fill = "rtl"
        for k in range(i, j):
            resolved[k] = fill
        i = j
    return resolved


def _page_is_rtl(raw_words: list[dict]) -> bool:
    """True if the majority of letter characters on a page are RTL.

    Scans ALL words on the page to make a single, stable decision.
    This is robust against per-line fluctuations in mixed-script text.
    """
    rtl = 0
    ltr = 0
    for w in raw_words:
        for ch in w.get("text", ""):
            bidi = unicodedata.bidirectional(ch)
            if bidi in _RTL_BIDI_TYPES:
                rtl += 1
            elif bidi == "L":
                ltr += 1
    return rtl > ltr


# ---------------------------------------------------------------------------
# Bracket / parenthesis mirroring canonicalization
# ---------------------------------------------------------------------------
# Different PDF producers bake in mirrored bracket codepoints for RTL
# rendering: one file may literally contain '(' where the other
# contains ')' at a parallel position, and '[[' vs ']]'.  These are
# not content differences -- they are an artefact of how each producer
# chose to encode the visually-mirrored glyph.  We canonicalize all
# bracket-class codepoints to a single direction at normalization time
# so they can never cause false diffs.
_BRACKET_MIRROR_MAP = str.maketrans({
    ")": "(",
    "]": "[",
    "}": "{",
    "\u00bb": "\u00ab",
    "\u203a": "\u2039",
    "\u2019": "\u2018",
    "\u201d": "\u201c",
    "\ufd3f": "\ufd3e",
    "\u207e": "\u207d",
    "\u208e": "\u208d",
})

# Words on the same line can sit at slightly different `top` values due
# to font metrics/baseline jitter. Cluster anything within this many
# PDF points as "the same line".
LINE_Y_TOLERANCE = 3.0

# --- Column-band detection tuning -----------------------------------
# Minimum width (in PDF points) an interior gap must have to count as
# a real column gutter, as opposed to ordinary word/sentence spacing.
# Real gutters between newspaper-style columns are typically 20-40pt;
# normal word spacing at 10-12pt body text is a few points. 12pt gives
# comfortable margin above word spacing without needing a huge gutter.
#
# This is only the FALLBACK, used when a page doesn't have enough data
# to measure its own typical word spacing (see `_typical_word_gap` /
# `_adaptive_min_gutter`) -- a fixed absolute point size cannot be
# right for every document. A real observed case: a compact folded
# leaflet using a much smaller body font than 10-12pt had a genuine,
# completely unambiguous column gutter (zero rows crossed it) only
# 10pt wide -- narrower than this constant -- while that SAME page's
# actual word spacing was under 3pt. The flat 12pt minimum rejected
# the real gutter and collapsed the whole page to row-major order,
# interleaving two unrelated side-by-side sections word-by-word. Since
# gaps and gutters both scale with a document's font size, the minimum
# gutter width needs to scale with THAT page's own measured word
# spacing, not one constant tuned for a single font size.
MIN_COLUMN_GUTTER_WIDTH = 12.0

# A real column gutter must be at least this many multiples of the
# page's own typical inter-word gap -- comfortably distinct from
# ordinary word spacing on THAT page, whatever font size it uses.
# MVP-20: lowered from 4.0 after a real observed case -- a genuine,
# completely clean (zero rows crossing) 8pt column gutter, on a zone
# whose own typical word gap was 2.3pt (ratio 3.48) -- was rejected by
# the 4.0 multiplier (needing >= 9.2pt) even after per-zone detection
# correctly isolated it from the rest of the page. 3.5 is the smallest
# reduction that accepts this real gutter while keeping the full
# existing test suite (including the narrower, small-font Arabic-
# leaflet case TEST 12 exists for) passing unchanged.
GUTTER_WIDTH_MULTIPLIER = 3.5

# Absolute floor under the adaptive threshold above, regardless of how
# tight a page's measured word-spacing is -- guards against a few
# noisy/rounded near-zero gap measurements producing a pathologically
# small "gutter" requirement.
MIN_GUTTER_WIDTH_FLOOR = 6.0
# ...lowered, for tiny type, to this many text heights.
_GUTTER_FLOOR_HEIGHTS = 1.5

# A within-line gap wider than this is excluded when measuring a
# page's "typical" word spacing -- a genuinely wide gap here is either
# already a candidate gutter or an unrelated layout artifact, and
# either way would skew the typical-spacing estimate upward, making
# the adaptive threshold too permissive right when it needs to be
# strictest.
_TYPICAL_GAP_OUTLIER_CUTOFF = 15.0

# Fraction of a page's rows that are allowed to bridge a candidate
# gutter (e.g. a title, a table rule, a stray wide word) before that
# x-range is no longer treated as a persistent column gutter. Kept
# small and floored at 1 row so a single spanning header line never
# blocks detection of otherwise-clean column gutters.
COLUMN_GUTTER_ROW_TOLERANCE_FRACTION = 0.06
# In a block of fewer rows than this, a gutter some row crosses is never
# wider than this (see `_detect_column_bands`).
_MAX_CROSSED_GUTTER_WIDTH = 60.0
_FEW_ROWS = 8

# Hard cap on how many column bands we'll ever produce for one page.
# Guards against pathological documents (e.g. a dense table) causing
# runaway over-splitting; real print layouts essentially never exceed
# this.
MAX_COLUMN_BANDS = 16

# --- Table-aware ordering (MVP-7) ------------------------------------
# The MVP-5 column-band heuristic is geometry-only and had one real
# blind spot: a wide ruled table's internal cell gutters can masquerade
# as column gutters, slicing the table into the wrong reading order,
# and a line-end hyphen in one table cell could then be stitched onto
# the first word of the NEXT cell ("double-" + "When" -> "doubleWhen"),
# fabricating a phantom token that matches nothing on the other side.
# The fix below detects real tables FIRST (ruling lines), carves
# their words out of the column-band logic, and orders each table
# cell-by-cell (row-major).

# Words whose center lands within a detected table bbox (plus this
# slack) are treated as table content, so words sitting exactly on a
# ruling line still join the table.
TABLE_ASSIGN_PAD = 2.0

# find_tables settings: ruling lines ONLY. A borderless-table fallback
# (pdfplumber's "text" strategy) was tried and deliberately rejected:
# word-alignment grids are geometrically indistinguishable from a
# legitimate multi-column body layout (aligned left edges, regular
# gutters), so it misdetects justified paragraphs and real 3-column
# labels as tables -- exactly the false positive this milestone exists
# to remove. Ruled tables (the norm for clinical-trial/MedDRA-style
# tables in the target documents) are detected exactly; borderless
# tables remain a documented known limitation for a future slice.
_TABLE_SETTINGS_LINES = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_y_tolerance": 1,
    "snap_x_tolerance": 2,
    "join_x_tolerance": 2,
    "join_y_tolerance": 1,
    "intersection_x_tolerance": 3,
    "intersection_y_tolerance": 3,
}

# --- Unicode normalization (MVP-7) ------------------------------------
# PDF text layers routinely smuggle in invisible/ambiguous characters
# that make two *visually identical* documents diff as different:
#   * zero-width chars (\u200b-\u200d, \ufeff) -- glue/spacers some
#     PDF producers emit between characters;
#   * soft hyphens (\u00ad) -- invisible at render time;
#   * non-breaking spaces (\u00a0) -- pdfplumber does not split words
#     on NBSP, so "5\u00a0mg" can survive as one word on one side and
#     "5 mg" as two on the other;
#   * ligatures (\ufb00-\ufb06: ff/fi/fl/ffi/ffl/st) -- a pure
#     font-level cosmetic that must never affect the diff result.
# Every word is normalized ONCE at extraction time, so both documents
# pass through the exact same pipeline before comparison.
_ZERO_WIDTH_OR_SOFT_HYPHEN_RE = re.compile("[\u200b-\u200d\ufeff\u00ad]")
_LIGATURE_MAP = str.maketrans(
    {
        "\ufb00": "ff",  # ﬀ
        "\ufb01": "fi",  # ﬁ
        "\ufb02": "fl",  # ﬂ
        "\ufb03": "ffi",  # ﬃ
        "\ufb04": "ffl",  # ﬄ
        "\ufb05": "st",  # ﬅ
        "\ufb06": "st",  # ﬆ
    }
)


_SCRIPT_CHARS_RE = re.compile("([" + re.escape("".join(sorted(SCRIPT_CHARS))) + "]+)")


def _nfkc_keeping_scripts(text: str) -> str:
    """NFKC everything except superscript/subscript characters, which
    NFKC would fold to plain baseline text ("2⁴" -> "24")."""
    if not any(ch in SCRIPT_CHARS for ch in text):
        return unicodedata.normalize("NFKC", text)
    parts = _SCRIPT_CHARS_RE.split(text)
    # re.split with one capture group: odd indices are the kept runs.
    return "".join(
        part if i % 2 else unicodedata.normalize("NFKC", part)
        for i, part in enumerate(parts)
    )


def _normalize_word_text(text: str, *, mirror_brackets: bool = False) -> str:
    """Strip invisible PDF-text-layer artifacts from one extracted word.

    Uses NFKC (compatibility decomposition + canonical composition)
    instead of NFC so that Arabic/Latin presentation-form ligatures
    (e.g. U+FEFB lam-alef, U+FB01 fi) are decomposed to their base
    letters.  NFC treats these as distinct from the spelled-out base
    sequence; NFKC does not, which is correct for comparison.

    ``mirror_brackets`` must be True ONLY for words genuinely in RTL
    script -- bracket-mirroring is a real-world fix for producers that
    bake in mirrored bracket codepoints when rendering RTL text, but
    applying it unconditionally corrupts perfectly ordinary LTR
    parenthetical text: every ")" in the word becomes "(", so an
    ordinary regulatory cross-reference like "(2.1)" is silently
    rewritten to "(2.1(" -- wrong on its own, and a guaranteed false
    diff against any other extractor (e.g. core.document_extractor's
    DOCX/PPTX/XLSX path) that doesn't happen to apply the exact same
    corruption.
    """
    text = _nfkc_keeping_scripts(text)
    text = (
        text.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u02bb", "'")
        .replace("\u02bc", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u201f", '"')
    )
    # Normalize bullet, list, and dingbat symbols across PDF/Word formats
    text = (
        text.replace("\uf0b7", "•")
        .replace("\uf06c", "•")  # Wingdings "l" (a round bullet) as a private-use code
        .replace("\u00b7", "•")
        .replace("\u25cf", "•")
        .replace("\u2022", "•")
        .replace("\uf0a7", "▪")
        .replace("\u25a0", "▪")
        .replace("\u25aa", "▪")
        .replace("\uf06e", "▪")
        .replace("\uf0fc", "✓")
        .replace("\uf0fb", "✗")
        .replace("\uf0fe", "☑")
        .replace("\uf0fd", "☒")
    )
    text = _ZERO_WIDTH_OR_SOFT_HYPHEN_RE.sub("", text)
    text = text.replace("\u00a0", " ")  # NBSP -> plain space
    text = text.translate(_LIGATURE_MAP)
    if mirror_brackets:
        text = text.translate(_BRACKET_MIRROR_MAP)
    return text


@dataclass
class Word:
    """A single extracted word with its exact position on the page.

    Coordinates are in pdfplumber's space: PDF points, origin at the
    page's top-left, y increasing downward -- ready to feed straight
    into a PyMuPDF/QGraphicsView render at the same DPI later for the
    "click a diff, jump to the word" feature.
    """

    text: str
    page: int  # 1-indexed
    x0: float
    top: float
    x1: float
    bottom: float
    # For words inside a detected table: (table_index, row, col) of the
    # grid cell containing the word, else None. Used to order table
    # words cell-by-cell and to stop hyphen-stitching across cell
    # boundaries (see build_comparison_tokens).
    cell_id: Optional[tuple[int, int, int]] = None
    # In an artwork sheet's box (product name, code, colours, approvals),
    # read at the page edge (`_page_edge_of_wide_table`).
    artwork: bool = False

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.top, self.x1, self.bottom)


# --- Superscript / subscript detection -------------------------------
# A PDF has no "superscript" flag: a raised/lowered glyph is just a
# smaller character drawn with its baseline shifted. Left alone,
# pdfplumber flattens a superscript into its neighbor ("2" + raised "4"
# reads as "24", identical to a real 24), and a subscript lowered more
# than y_tolerance falls out of its line entirely ("h" + lowered "2"
# ends up as a stray "2" on a line of its own).
#
# A character counts as raised/lowered only when it TOUCHES a larger
# character (its base) and its baseline differs from the base's. A
# smaller character on the SAME baseline (small caps, a mixed-size
# run) stays normal text -- but is still moved onto its base's line,
# since its lower top would otherwise split "S" + "MALL" into two words.

# Script glyphs are at most this fraction of their base's size (typical
# producers use ~0.58-0.67).
_SCRIPT_MAX_SIZE_RATIO = 0.85
# ...and at least this fraction, and shifted by at most _SCRIPT_MAX_RISE
# of the base size: a drop cap is several times the body size and sits
# lines away from the body text beside it, which is not a base/script pair.
_SCRIPT_MIN_SIZE_RATIO = 0.4
_SCRIPT_MAX_RISE = 0.6
# Minimum baseline shift, in points, to count as raised/lowered. Real
# subscripts were observed lowered by as little as 0.48pt.
_SCRIPT_MIN_RISE = 0.25
# Max horizontal gap between a script glyph and the glyph it touches.
_SCRIPT_MAX_GAP = 1.5
# ...or, from a script glyph to its BASE, up to this fraction of the
# base's size (about one space): an SPL printed from a browser sets
# "C<sub>max</sub>" and "D<sub>2</sub>" a space's width after the base
# (1.9pt at 7.4pt), and an undetected "max"/"2" read as a word of its own
# made "C max" a critical rewording of "Cmax". The size and baseline
# tests still decide; the index below reaches about 2.5pt either way.
_SCRIPT_BASE_GAP_EM = 0.3
# Width (pt) of the horizontal buckets used to skip glyphs with no larger
# glyph close by. Neighbouring buckets are checked too, so a base up to
# this far away is always seen (glyphs touch within _SCRIPT_MAX_GAP).
_SIZE_BUCKET_WIDTH = 40.0


# A digit set at the text's own size but raised by this share of it, right
# after a letter, is a superscript (a Word export raises "2" in "m2"
# without making it smaller).
_RAISED_DIGIT_MIN_RISE = 0.2
_RAISED_DIGIT_MAX_RISE = 0.6
_RAISED_DIGIT_MAX_RUN = 3


def _mark_raised_digits(chars: list[dict]) -> None:
    """Tag, in place, digits raised at full size right after a non-digit
    -- "kg/m2" exported with the "2" lifted but not shrunk read "30kg/m 2,"
    against "30kg/m²," -- as superscripts, on their base's line. Only
    digits, at most `_RAISED_DIGIT_MAX_RUN` in a row, in the content
    stream right after the glyph they belong to."""
    k = 1
    while k < len(chars):
        base, c = chars[k - 1], chars[k]
        size = float(c.get("size") or 0.0)
        if (
            c["text"].isdigit() and base["text"].strip() and not base["text"].isdigit()
            and not c.get("script") and c.get("upright", True) and base.get("upright", True)
            and size and abs(float(base.get("size") or 0.0) - size) < 0.1
            and -0.5 <= c["x0"] - base["x1"] <= 1.0
        ):
            rise = c["matrix"][5] - base["matrix"][5]
            if _RAISED_DIGIT_MIN_RISE * size <= rise <= _RAISED_DIGIT_MAX_RISE * size:
                run = [c]
                j = k + 1
                while (j < len(chars) and len(run) < _RAISED_DIGIT_MAX_RUN and chars[j]["text"].isdigit()
                       and abs(chars[j]["matrix"][5] - c["matrix"][5]) < 0.1):
                    run.append(chars[j])
                    j += 1
                if j < len(chars) and chars[j]["text"].isdigit() and abs(chars[j]["matrix"][5] - c["matrix"][5]) < 0.1:
                    k = j  # a longer raised number: not a superscript
                    continue
                for d in run:
                    d["script"] = SUPERSCRIPT
                    for key in ("top", "bottom", "doctop", "y0", "y1", "height"):
                        if key in base:
                            d[key] = base[key]
                k = j
                continue
        k += 1


def _mark_script_chars(chars: list[dict]) -> None:
    """Tag raised/lowered characters on one page, in place.

    Each detected char gets ``"script"`` set to "sup"/"sub", and its
    vertical geometry replaced with its base character's, so it groups
    into the same line and word as the text it belongs to (a word's
    box then spans the base line height, which is also what a
    highlight should cover). Same-baseline smaller chars get the base
    geometry too, without a ``"script"`` tag.
    """
    # Spaces take part only as run continuations (below), never as a
    # base: a space set in the script size after a subscript must move
    # with it, or the word break it provides is lost.
    upright = [c for c in chars if c.get("upright", True)]
    if not upright:
        return
    _mark_raised_digits(chars)
    # Index non-space glyphs by (rounded edge x, vertical band) so each
    # lookup only checks glyphs near that edge on nearby lines. Two
    # vertically overlapping glyphs have centers less than one band
    # (the tallest glyph's height) apart, so bands +-1 cover them.
    band_height = max(c["bottom"] - c["top"] for c in upright) or 1.0

    def band(c: dict) -> int:
        return int((c["top"] + c["bottom"]) / 2 // band_height)

    # Largest glyph per (band, x bucket): a base touches its script
    # glyph, so only the glyph's own and adjacent buckets matter.
    bands: dict[int, int] = {}
    area_max_size: dict[tuple[int, int], float] = {}
    for c in upright:
        b = bands[id(c)] = int((c["top"] + c["bottom"]) / 2 // band_height)
        if c["text"].strip():
            area = (b, int(c["x0"] // _SIZE_BUCKET_WIDTH))
            size = c["size"]
            if size > area_max_size.get(area, 0.0):
                area_max_size[area] = size

    def neighbors(c: dict, left: bool, base: bool = False) -> list[dict]:
        index, edge = (by_x1, round(c["x0"])) if left else (by_x0, round(c["x1"]))
        c_band = band(c)
        found = []
        for x in range(edge - 2, edge + 3):
            for b in (c_band - 1, c_band, c_band + 1):
                for d in index.get((x, b), ()):
                    gap = c["x0"] - d["x1"] if left else d["x0"] - c["x1"]
                    limit = max(_SCRIPT_MAX_GAP, _SCRIPT_BASE_GAP_EM * d["size"]) if base else _SCRIPT_MAX_GAP
                    if d is not c and -_SCRIPT_MAX_GAP <= gap <= limit and (
                        d["top"] < c["bottom"] and c["top"] < d["bottom"]
                    ):
                        found.append(d)
        return found

    def baseline(c: dict) -> float:
        return c["matrix"][5]  # PDF space, y up

    def stacked_under(c: dict, d: dict) -> bool:
        """Whether ``c`` sits under (or over) a glyph of ``d``'s line --
        sharing more than half its width -- rather than beside ``d``: a
        footnote "a" opening the next line, right under the "S" of "SD:",
        is not that line's subscript."""
        width = c["x1"] - c["x0"]
        if width <= 0:
            return False
        c_band = band(c)
        for x in range(int(c["x0"]) - 3, int(c["x1"]) + 2):
            for b in (c_band - 1, c_band, c_band + 1):
                for e in by_x0.get((x, b), ()):
                    if (e is not c and abs(baseline(e) - baseline(d)) < 0.1
                            and min(c["x1"], e["x1"]) - max(c["x0"], e["x0"]) > 0.5 * width):
                        return True
        return False

    # Sizes of glyphs already attached to a base; only a glyph of one of
    # these sizes can continue a run.
    attached_sizes: set[float] = set()

    # The largest glyph in the 3x3 block of areas around one area. Every
    # glyph on the same line in the same bucket shares it, so it is
    # worked out once per area rather than once per glyph (a 100-page
    # document has two million glyphs and a few thousand areas).
    neighborhood_max: dict[tuple[int, int], float] = {}

    def nearby_max_size(c_band: int, bucket: int) -> float:
        key = (c_band, bucket)
        found = neighborhood_max.get(key)
        if found is None:
            found = max(
                area_max_size.get((b, x), 0.0)
                for b in (c_band - 1, c_band, c_band + 1)
                for x in (bucket - 1, bucket, bucket + 1)
            )
            neighborhood_max[key] = found
        return found

    # Most glyphs (ordinary body text) have no noticeably larger glyph
    # anywhere near them, so they cannot be raised or lowered. One can
    # still CONTINUE a run, but only at the size of a glyph that could
    # attach to a base -- so everything else is left out before sorting.
    may_attach: dict[int, bool] = {}
    for c in upright:
        nearby_max = nearby_max_size(bands[id(c)], int(c["x0"] // _SIZE_BUCKET_WIDTH))
        may_attach[id(c)] = bool(c["text"].strip()) and c["size"] <= nearby_max * _SCRIPT_MAX_SIZE_RATIO
    if not any(may_attach.values()):
        return  # nothing on this page can be a superscript or subscript

    # Glyphs by (rounded edge x, band), for the neighbour search -- built
    # only for a page that has a candidate (most pages have none).
    by_x1: dict[tuple[int, int], list[dict]] = {}
    by_x0: dict[tuple[int, int], list[dict]] = {}
    for c in upright:
        if c["text"].strip():
            b = bands[id(c)]
            by_x1.setdefault((round(c["x1"]), b), []).append(c)
            by_x0.setdefault((round(c["x0"]), b), []).append(c)
    run_sizes = {round(c["size"], 1) for c in upright if may_attach[id(c)]}
    candidates_in_order = sorted(
        (c for c in upright if may_attach[id(c)] or round(c["size"], 1) in run_sizes),
        key=lambda c: c["x0"],
    )

    # Left to right, so a multi-glyph run ("10" raised in "x10") can
    # inherit from the glyph before it.
    for c in candidates_in_order:
        size = c["size"]
        can_have_base = may_attach[id(c)]
        if not can_have_base and round(size, 1) not in attached_sizes:
            continue
        base = None
        if can_have_base:
            # A base that touches first; one a space away only if none does.
            close = neighbors(c, left=True) + neighbors(c, left=False)
            candidates = close + [
                d for d in neighbors(c, left=True, base=True) + neighbors(c, left=False, base=True)
                if all(d is not e for e in close)
            ]
        else:
            candidates = []
        # Of the glyphs it could be raised or lowered from, the one whose
        # line it overlaps most: a footnote "a" between two lines of a
        # table note reads as lowered from "SD:" above ("SₐD:") and raised
        # from "Difference" below -- it sits on the line below.
        fits = [
            d for d in candidates
            if d["size"] * _SCRIPT_MIN_SIZE_RATIO <= size <= d["size"] * _SCRIPT_MAX_SIZE_RATIO
            and abs(baseline(c) - baseline(d)) <= d["size"] * _SCRIPT_MAX_RISE
            and not stacked_under(c, d)
        ]
        if fits:
            base = max(
                fits,
                key=lambda d: (min(c["bottom"], d["bottom"]) - max(c["top"], d["top"]), -fits.index(d)),
            )
        if base is None:
            # Continuation of a run already attached on its left.
            for d in neighbors(c, left=True):
                if (
                    "_base" in d
                    and abs(d["size"] - size) < 0.1
                    and abs(baseline(d) - baseline(c)) < 0.1
                ):
                    if d.get("script"):
                        c["script"] = d["script"]
                    c["_base"] = d["_base"]
                    attached_sizes.add(round(size, 1))
                    break
            else:
                continue
        else:
            rise = baseline(c) - baseline(base)
            if rise >= _SCRIPT_MIN_RISE:
                c["script"] = SUPERSCRIPT
            elif rise <= -_SCRIPT_MIN_RISE:
                c["script"] = SUBSCRIPT
            c["_base"] = base.get("_base", base)
            attached_sizes.add(round(size, 1))

    attached_map: dict[int, list[dict]] = {}
    for c in upright:
        base = c.pop("_base", None)
        if base is not None and base is not c:
            for key in ("top", "bottom", "doctop", "y0", "y1", "height"):
                if key in base:
                    c[key] = base[key]
            if "fontname" in base:
                c["fontname"] = base["fontname"]
            attached_map.setdefault(id(base), []).append(c)

    if attached_map:
        # Reposition attached script characters so they are adjacent to base in chars stream,
        # preventing out-of-order PDF content stream emissions from splitting script words.
        attached_ids = {id(c) for clist in attached_map.values() for c in clist}
        new_chars = []
        for c in chars:
            if id(c) in attached_ids:
                continue
            scripts = attached_map.get(id(c))
            if scripts:
                left_scripts = sorted([s for s in scripts if s["x0"] < c["x0"]], key=lambda s: s["x0"])
                right_scripts = sorted([s for s in scripts if s["x0"] >= c["x0"]], key=lambda s: s["x0"])
                new_chars.extend(left_scripts)
                new_chars.append(c)
                new_chars.extend(right_scripts)
            else:
                new_chars.append(c)
        chars[:] = new_chars


def extract_words(
    pdf_path: str,
    first_page: int = 1,
    last_page: Optional[int] = None,
    table_layout: Optional[dict[int, list[dict]]] = None,
    crop: Optional[dict[int, tuple]] = None,
) -> list[Word]:
    """Extract every word in a PDF, in column-aware reading order.

    Args:
        pdf_path: Path to the PDF file.
        first_page, last_page: 1-indexed, inclusive page range (default:
            the whole document). Pages are processed independently, so
            ranges can be extracted in parallel and concatenated.
        table_layout: When given, filled with each page's tables as the
            reading order used them -- {page: [{"bbox", "cells",
            "status", "parent"}]} in the same coordinates as the words
            (see `_order_page_words`). The viewer draws them.
        crop: Margins cut off per page, {page: (header_bottom, footer_top,
            left, right)} in the same coordinates (the Crop Margins
            dialog's): only what lies inside is read, and the page's
            columns and tables are worked out from that alone.

    Returns:
        A flat list of Word objects covering all pages, ordered
        page-by-page, then column-by-column (left to right), then
        top-to-bottom within each column, then left-to-right within
        each line. For a single-column page this is identical to
        simple top-to-bottom, left-to-right order.
    """
    all_words: list[Word] = []

    with _open_pdf_pages(pdf_path) as (page_count, get_page):
        last = page_count if last_page is None else min(last_page, page_count)
        for page_idx in range(first_page, last + 1):
            page = get_page(page_idx - 1)
            # A PDF's own content-stream coordinates are relative to its
            # MediaBox/CropBox origin, which is NOT always (0, 0) --
            # professionally-produced print/packaging artwork routinely
            # defines a MediaBox with a negative or offset origin (bleed
            # area around the trim box). pdfplumber's raw x0/top are
            # reported directly in that native space, but PyMuPDF's
            # rendering (ui/document_panel.py's render_pdf, which this
            # module's coordinates get overlaid onto for highlighting)
            # always normalizes so the RENDERED page starts at (0, 0),
            # regardless of the underlying box origin. Without
            # correcting for this, every highlight on such a PDF lands
            # OFFSET by a constant amount from the real text -- a real
            # observed case: a page with MediaBox origin (-9, -9) had
            # every highlight land about 9pt (25px at this app's render
            # DPI) away from the actual word. Subtracting the page's
            # own top-left corner (`page.bbox`, pdfplumber's own
            # reported origin) from every word's coordinates normalizes
            # them to the same (0, 0)-origin space PyMuPDF renders in --
            # verified to exactly match PyMuPDF's own text coordinates
            # for the same word on the real reported file.
            page_x0, page_top0 = page.bbox[0], page.bbox[1]
            # extra_attrs is NOT optional tuning here: pdfplumber groups
            # characters by (upright, *extra_attrs) before splitting them
            # into words (utils/text.py, WordExtractor.iter_extract_tuples),
            # so chars of different fonts/sizes are kept in separate
            # groups. Dropping it merges differently-styled runs (e.g. the
            # letter-spaced drug name on label artwork) into one garbled
            # word stream and silently changes the diff result.
            # Remap Adobe Symbol / Wingdings / Dingbats / Webdings font glyphs lacking ToUnicode CMap
            for c in page.chars:
                font = (c.get("fontname") or "").lower()
                t = c.get("text")
                if "symbol" in font:
                    if t == "\u00b3":
                        c["text"] = "≥"
                    elif t == "\u00a3":
                        c["text"] = "≤"
                    elif t == "\u00b9":
                        c["text"] = "≠"
                    elif t in ("\u00b7", "·"):
                        c["text"] = "•"
                elif "wingding" in font or "dingbat" in font or "webding" in font:
                    if t in ("\u00a7", "§"):
                        c["text"] = "▪"  # Wingdings 0xA7 is small square bullet
                    elif t in ("\u00a8", "¨"):
                        c["text"] = "▫"  # Wingdings 0xA8 is empty small square
                    elif t in ("\u00fc", "ü"):
                        c["text"] = "✓"  # Wingdings 0xFC is checkmark
                    elif t in ("\u00fb", "û"):
                        c["text"] = "✗"  # Wingdings 0xFB is ballot X
                    elif t in ("\u00fe", "þ"):
                        c["text"] = "☑"  # Wingdings 0xFE is ballot box with check
                    elif t in ("\u00fd", "ý"):
                        c["text"] = "☒"  # Wingdings 0xFD is ballot box with X
                    elif t == "l":
                        c["text"] = "●"
                    elif t in ("n", "m"):
                        c["text"] = "■"
                    elif t == "u":
                        c["text"] = "◆"
                    elif t == "v":
                        c["text"] = "❖"
                    elif t == "w":
                        c["text"] = "⬥"
                    elif t in ("\u00b7", "·"):
                        c["text"] = "•"


            _mark_script_chars(page.chars)

            raw_words = page.extract_words(
                x_tolerance=2,
                y_tolerance=2,
                keep_blank_chars=False,
                use_text_flow=False,
                extra_attrs=["fontname"],
                return_chars=True,
            )
            for w in raw_words:
                chars = w.pop("chars")
                if any(c.get("script") for c in chars):
                    w["text"] = join_runs((c["text"], c.get("script")) for c in chars)
            pw, ph = float(page.width), float(page.height)
            # Filter words outside the page canvas (e.g. pasteboard artifacts
            # outside trim box). A word counts as on the page when its
            # CENTRE is: one more than half outside the visible (crop)
            # area cannot be read there -- a browser's print header placed
            # above an SPL page's crop box ("These highlights do not
            # include ... Page 4 of 34", 12pt tall, ending 4.7pt above the
            # page) was being compared as if it were printed on every page.
            canvas_words = [
                w for w in raw_words
                if w["x1"] > (page_x0 - 5.0)
                and w["x0"] < (page_x0 + pw + 5.0)
                and w["bottom"] > (page_top0 - 5.0)
                and w["top"] < (page_top0 + ph + 5.0)
                and page_x0 <= (w["x0"] + w["x1"]) / 2.0 <= page_x0 + pw
                and page_top0 <= (w["top"] + w["bottom"]) / 2.0 <= page_top0 + ph
            ]
            # Filter isolated running page numbers and technical plate slugs in margins
            filtered_raw_words = []
            page_str = str(page_idx)
            # Words by `top`, so a margin word's line-mates are found by
            # bisecting rather than by scanning every word on the page
            # (dense artwork has hundreds of words in its margins). The
            # exact test is applied to the slice, in page order.
            by_top = sorted(range(len(canvas_words)), key=lambda k: canvas_words[k]["top"])
            tops = [canvas_words[k]["top"] for k in by_top]
            for w in canvas_words:
                rel_top = w["top"] - page_top0
                rel_bottom = w["bottom"] - page_top0
                if rel_top < 65.0 or rel_bottom > (ph - 65.0):
                    lo = bisect.bisect_left(tops, w["top"] - 6.0)
                    hi = bisect.bisect_right(tops, w["top"] + 6.0)
                    same_line_words = [
                        canvas_words[k] for k in sorted(by_top[lo:hi])
                        if abs(canvas_words[k]["top"] - w["top"]) <= 5.0
                    ]
                    line_txt = " ".join(ow["text"] for ow in same_line_words)
                    if any(k in line_txt for k in ["Dimensions:", "Color:", "Pantone"]):
                        continue
                    if w["text"].strip() == page_str and len(same_line_words) <= 1:
                        continue
                filtered_raw_words.append(w)

            # Rotated (non-upright) "words" here are pdfplumber's
            # degenerate one-character fragments (see the MVP-14 module
            # note above) -- never usable as-is. Keep only the upright
            # ones for the normal body/table/column pipeline, and
            # reconstruct rotated text separately, straight from
            # page.chars, which is what actually carries each
            # character's text matrix.
            # Cropped margins are gone before anything is worked out:
            # columns, zones and tables are found in what is kept, so a
            # cropped-away column (a side-by-side document's Notations
            # and Proposed Label) no longer shapes how the rest is read.
            kept = _kept_area(crop, page_idx, page_x0, page_top0)
            if kept is not None:
                filtered_raw_words = [w for w in filtered_raw_words if _centre_inside(w, kept)]
            upright_words = [w for w in filtered_raw_words if w.get("upright", True)]
            # Tables only decide how the page's upright words are read. A
            # page without any (a scan, a blank page, artwork) skips the
            # search -- which reads every vector path on the page, a
            # million of them on an outlined scan.
            tables = _detect_page_tables(page) if upright_words else []
            if kept is not None:
                tables = [clipped for clipped in (_clip_table(t, kept) for t in tables) if clipped is not None]
            # One RTL decision per page -- stable across renderings.
            page_rtl = _page_is_rtl(filtered_raw_words)
            page_layout: Optional[list[dict]] = [] if table_layout is not None else None
            ordered_words = _order_page_words(upright_words, tables, rtl=page_rtl, page=page, layout=page_layout)
            if table_layout is not None:
                table_layout[page_idx] = [_shift_table(t, page_x0, page_top0) for t in page_layout]
            rotated_chars = [
                c for c in page.chars
                if not c.get("upright", True) and (kept is None or _centre_inside(c, kept))
            ]
            if rotated_chars and hasattr(page, "word_box_of"):
                # The glyph stream leaves most word spaces out; MuPDF's own
                # words say where turned words end.
                for c in rotated_chars:
                    c["word_box"] = page.word_box_of(c)
            ordered_words.extend(_ordered_rotated_words(rotated_chars))
            for w in ordered_words:
                raw_text = w["text"]
                # pdfplumber merges chars ascending by x0, which reverses
                # the letters inside RTL words. Detect and undo for RTL words.
                is_rtl_word = _text_is_rtl(raw_text)
                if is_rtl_word:
                    raw_text = _fix_rtl_word_chars(raw_text)
                text = _normalize_word_text(raw_text, mirror_brackets=is_rtl_word)
                if not text.strip():
                    # Nothing but invisible characters: a soft hyphen drawn
                    # on its own after "N,N" at a line end. An empty word
                    # can be neither matched nor listed.
                    continue

                all_words.append(
                    Word(
                        text=text,
                        page=page_idx,
                        x0=w["x0"] - page_x0,
                        top=w["top"] - page_top0,
                        x1=w["x1"] - page_x0,
                        bottom=w["bottom"] - page_top0,
                        cell_id=w.get("cell_id"),
                        artwork=bool(w.get("artwork")),
                    )
                )
    return all_words


def _kept_area(
    crop: Optional[dict[int, tuple]], page_idx: int, dx: float, dy: float
) -> Optional[tuple[float, float, float, float]]:
    """The part of page ``page_idx`` a crop keeps, (x0, top, x1, bottom)
    in the page's own coordinates (the crop is given in the words'
    (0, 0)-origin space), or None when the page is not cropped."""
    if not crop:
        return None
    margins = crop.get(page_idx)
    if not margins:
        return None
    header_bottom, footer_top = float(margins[0]), float(margins[1])
    left = float(margins[2]) if len(margins) >= 4 else 0.0
    right = float(margins[3]) if len(margins) >= 4 else float("inf")
    return (left + dx, header_bottom + dy, right + dx, footer_top + dy)


def _centre_inside(obj: dict, area: tuple[float, float, float, float]) -> bool:
    x = (obj["x0"] + obj["x1"]) / 2.0
    y = (obj["top"] + obj.get("bottom", obj["top"])) / 2.0
    return area[0] <= x <= area[2] and area[1] <= y <= area[3]


# A cell cut down to less than this (pt) by a crop is gone.
_MIN_CLIPPED_CELL = 2.0


def _clip_table(table: dict, area: tuple[float, float, float, float]) -> Optional[dict]:
    """A detected table cut to the kept part of a cropped page: cells
    outside it are dropped (rows and columns left empty go too), cells
    across its edge are trimmed. None when nothing of the table is left."""
    def clip(box):
        if box is None:
            return None
        x0, top, x1, bottom = max(box[0], area[0]), max(box[1], area[1]), min(box[2], area[2]), min(box[3], area[3])
        if x1 - x0 < _MIN_CLIPPED_CELL or bottom - top < _MIN_CLIPPED_CELL:
            return None
        return (x0, top, x1, bottom)

    grid = [[clip(cell) for cell in row] for row in table["cells"]]
    grid = [row for row in grid if any(cell is not None for cell in row)]
    if not grid:
        return None
    width = max(len(row) for row in grid)
    keep_cols = [c for c in range(width) if any(c < len(row) and row[c] is not None for row in grid)]
    grid = [[row[c] if c < len(row) else None for c in keep_cols] for row in grid]
    bbox = clip(table["bbox"])
    if bbox is None:
        return None
    # A table the crop cut down (a side-by-side grid kept to its RLD
    # column) is still that table: what is kept of it is read as its
    # cells, as without the crop -- not as loose text because only one
    # column is left.
    columns = max((len(row) for row in table["cells"]), default=0)
    cut_down = columns >= 2 and len(table["cells"]) >= 3 and len(keep_cols) < columns
    return dict(table, bbox=bbox, cells=grid, cut_down=cut_down)


def _shift_table(table: dict, dx: float, dy: float) -> dict:
    """A `layout` table moved into the words' (0, 0)-origin page space."""
    def move(box):
        return None if box is None else (box[0] - dx, box[1] - dy, box[2] - dx, box[3] - dy)

    return dict(table, bbox=move(table["bbox"]), cells=[[move(cell) for cell in row] for row in table["cells"]])


def _order_page_words(
    raw_words: list[dict],
    tables: Optional[list[dict]] = None,
    *,
    rtl: bool = False,
    page: Optional[Any] = None,
    layout: Optional[list[dict]] = None,
) -> list[dict]:
    """Return one page's word-dicts in reading order.

    Words inside detected tables are ordered cell-by-cell (row-major:
    rows top-to-bottom, cells left-to-right within a row, words
    top-to-bottom within a cell). Non-table words keep the MVP-5
    column-band ordering, and each table's rows are interleaved back
    into their column band by vertical position so a table in the
    middle of a page does not push surrounding text out of order.

    Args:
        raw_words: Output of ``page.extract_words()`` for one page.
        tables: Output of ``_detect_page_tables()`` for the same page
            (may be empty/None).
        rtl: If True, words within each line are sorted right-to-left
            (descending x0) instead of left-to-right.  Determined
            at page level by the caller.
        layout: When given, gets one dict per detected table: its
            "bbox" and "cells" (row-major, None where merged away), the
            table it is drawn inside ("parent", or None), and "status":
            "table" (read cell by cell), "nested" (read cell by cell
            inside its parent's cell) or "ignored" (not a real table --
            ruling lines over flowing text, one column -- read as text).

    Returns:
        The same word-dicts, reordered into reading order. Dicts for
        table words gain a "cell_id" key: (table_index, row, col).
    """
    if not raw_words:
        return []

    tables = tables or []

    # Content bounds over ALL words (body + table) so column-band ranges
    # still span the table even when body text is narrower than it.
    content_x0 = min(w["x0"] for w in raw_words)
    content_x1 = max(w["x1"] for w in raw_words)

    # A table drawn inside a cell of another (a side-by-side document's
    # "RLD Label | Notations | Proposed Label" grid with an adverse-
    # reactions table in its first cell) is NESTED: the outer table
    # claims the words, and the inner table only says which of its own
    # cells each one is in -- see `_table_units_for`.
    parents = _nesting_parents(tables)
    top_level = [t_idx for t_idx in range(len(tables)) if t_idx not in parents]

    def real_tables(candidates: list[int], ids: dict) -> set[int]:
        return {
            t_idx
            for t_idx in candidates
            if (_looks_like_real_table(t_idx, ids) or tables[t_idx].get("cut_down"))
            and not _cuts_through_words(tables[t_idx], raw_words)
        }

    cell_ids = _assign_words_to_cells(raw_words, tables, only=top_level)
    real_table_idxs = real_tables(top_level, cell_ids)
    cell_ids = {w_idx: cid for w_idx, cid in cell_ids.items() if cid[0] in real_table_idxs}
    # A table inside one that is not a real table stands on its own.
    orphans = [j for j, parent in parents.items() if parent not in real_table_idxs and parents.get(parent) is None]
    if orphans:
        orphan_ids = _assign_words_to_cells(raw_words, tables, only=orphans, taken=set(cell_ids))
        kept = real_tables(orphans, orphan_ids)
        cell_ids.update({w_idx: cid for w_idx, cid in orphan_ids.items() if cid[0] in kept})
        real_table_idxs |= kept
    inner_ids = _nested_cell_ids(raw_words, tables, parents, cell_ids, real_table_idxs)

    # Tag the word dicts themselves so extract_words() can carry each
    # word's cell membership onto the Word objects (the hyphen-stitch
    # guard in build_comparison_tokens depends on it). Body words are
    # left without the key -> cell_id None. A nested table's word carries
    # its own (inner) cell, so nothing is stitched across inner cells.
    for w_idx, cid in cell_ids.items():
        raw_words[w_idx]["cell_id"] = inner_ids.get(w_idx, cid)

    if layout is not None:
        nested_used = {cid[0] for cid in inner_ids.values()}
        for t_idx, table in enumerate(tables):
            status = (
                "table" if t_idx in real_table_idxs
                else "nested" if t_idx in nested_used
                else "ignored"
            )
            layout.append(
                {"bbox": table["bbox"], "cells": table["cells"], "status": status, "parent": parents.get(t_idx)}
            )

    body_words = [w for i, w in enumerate(raw_words) if i not in cell_ids]
    if not body_words:
        table_units: list[tuple[int, float, list[dict]]] = []
        for t_idx in range(len(tables)):
            entries = [
                (i, w)
                for i, w in enumerate(raw_words)
                if cell_ids.get(i, (None, None, None))[0] == t_idx
            ]
            if entries:
                table_units.extend(
                    _table_units_for(
                        t_idx, entries, [(content_x0, content_x1)], cell_ids, tables[t_idx]["bbox"], rtl=rtl,
                        inner_ids=inner_ids, tables=tables,
                    )
                )
        return [w for _, _, words in sorted(table_units, key=lambda u: u[1]) for w in words]

    rows = sorted(_group_into_lines(body_words), key=_line_top)
    page_bands = _detect_column_bands(
        rows, content_x0, content_x1, min_gutter=_adaptive_min_gutter(rows)
    )
    page_bands = _merge_narrow_column_bands(page_bands, min_width=25.0)
    page_bands = _merge_label_strips(page_bands, rows)
    is_page_cols = len(page_bands) >= 2 and _is_robust_page_columns(page_bands, content_x0, content_x1)
    if not is_page_cols:
        page_panels = _detect_page_panels(rows, content_x0, content_x1)
        if page_panels is not None:
            page_bands = page_panels
            is_page_cols = True

    # Lines printed across the columns right under a box at the top of the
    # page (an outsert's "Note: Pharma code/ Bar code ... visible on folded
    # leaflet." under its artwork box) belong with the box, not cut up
    # among the columns they cross.
    caption_units: list[tuple[int, float, list[dict]]] = []
    if is_page_cols:
        read_tables = {cid[0] for cid in cell_ids.values()}
        for t_idx, table in enumerate(tables):
            if t_idx not in read_tables:
                continue
            if _page_edge_of_wide_table(table["bbox"], page_bands, body_words) != "top":
                continue
            caption = _lines_under_box(table["bbox"], page_bands, rows)
            if caption:
                for line in caption:
                    for w in line:
                        w["artwork"] = True
                taken = {id(w) for line in caption for w in line}
                body_words = [w for w in body_words if id(w) not in taken]
                caption_units.extend((0, min(w["top"] for w in line) - _PAGE_EDGE_KEY, line) for line in caption)

    body_units, bands = _order_body_units(body_words, content_x0, content_x1, rtl=rtl, page=page)

    band_col: dict[int, int] = {}
    band_yranges: list[tuple[float, float]] = []
    for band_idx in range(len(bands)):
        b_words = [w for u in body_units if u[0] == band_idx for w in u[2]]
        if b_words:
            by0 = min(w["top"] for w in b_words)
            by1 = max(w.get("bottom", w["top"] + 10.0) for w in b_words)
            band_yranges.append((by0, by1))
            if is_page_cols:
                b_mid_x = sum((w["x0"] + w["x1"]) / 2.0 for w in b_words) / len(b_words)
                band_col[band_idx] = _band_index_for(b_mid_x, page_bands)
        else:
            band_yranges.append((0.0, 1e9))
            if is_page_cols:
                b_mid_x = (bands[band_idx][0] + bands[band_idx][1]) / 2.0
                band_col[band_idx] = _band_index_for(b_mid_x, page_bands)

    table_units: list[tuple[int, float, list[dict]]] = []
    for t_idx in range(len(tables)):
        entries = [
            (i, w)
            for i, w in enumerate(raw_words)
            if cell_ids.get(i, (None, None, None))[0] == t_idx
        ]
        if not entries:
            continue
        t_box = tables[t_idx]["bbox"]
        t_mid_x = (t_box[0] + t_box[2]) / 2.0
        t_mid_y = (t_box[1] + t_box[3]) / 2.0

        if is_page_cols:
            t_col = _band_index_for(t_mid_x, page_bands)
            cand_bands = [b_idx for b_idx, c in band_col.items() if c == t_col]
            if not cand_bands:
                cand_bands = list(range(len(bands)))
        else:
            cand_bands = list(range(len(bands)))

        best_b = min(
            cand_bands,
            key=lambda b: max(band_yranges[b][0] - t_mid_y, 0.0, t_mid_y - band_yranges[b][1]),
        )
        units = _table_units_for(
            t_idx, entries, bands, cell_ids, t_box, band_idx=best_b, rtl=rtl,
            inner_ids=inner_ids, tables=tables,
        )
        edge = _page_edge_of_wide_table(t_box, page_bands, body_words) if is_page_cols else None
        if edge is not None:
            for _band, _top, words in units:
                for w in words:
                    w["artwork"] = True
        if edge == "top":
            units = [(0, top - _PAGE_EDGE_KEY, words) for _band, top, words in units]
        elif edge == "bottom":
            units = [(len(bands) - 1, top + _PAGE_EDGE_KEY, words) for _band, top, words in units]
        table_units.extend(units)

    all_units = body_units + table_units + caption_units
    ordered: list[dict] = []
    for band_idx in range(len(bands)):
        band_units = sorted(
            (u for u in all_units if u[0] == band_idx),
            key=lambda u: u[1],
        )
        for _, _, words in band_units:
            ordered.extend(words)
    return ordered


# Sort-key offset that puts a unit before (after) everything on its page.
_PAGE_EDGE_KEY = 1e6
# Text above (below) a page-top (-bottom) box, at most this share of the
# text below (above) it: a sheet label, not a column.
_EDGE_TABLE_STRAY_SHARE = 0.02


def _page_edge_of_wide_table(
    t_box: tuple[float, float, float, float],
    page_bands: list[tuple[float, float]],
    body_words: list[dict],
) -> Optional[str]:
    """"top" (or "bottom") when the table spans two or more of the page's
    columns with none of their text above (below) it: read whole at the
    start (end) of the page, not inside whichever column its middle is
    over. An outsert's artwork box (product name, code, colours, dates)
    across four columns at the top of the sheet was read between the end
    of the third column and the start of the fourth -- and two versions
    break their columns at different words, so the box split the text on
    both sides ("Table 7." / "V. No. : 01 CODE : 8099813 ... Table 8:")."""
    x0, top, x1, bottom = t_box
    spanned = [
        (lo, hi) for lo, hi in page_bands
        if min(x1, hi) - max(x0, lo) > 0.5 * (hi - lo)
    ]
    if len(spanned) < 2:
        return None
    lo_x, hi_x = min(lo for lo, _ in spanned), max(hi for _, hi in spanned)
    under = [w for w in body_words if lo_x <= (w["x0"] + w["x1"]) / 2.0 <= hi_x]
    above = [w for w in under if w.get("bottom", w["top"]) <= top + 1.0]
    below = [w for w in under if w["top"] >= bottom - 1.0]
    if len(above) + len(below) < len(under):
        return None  # text beside the box: it sits among the columns
    # A sheet label over the box ("1.14.2.2") does not make it mid-page.
    if below and len(above) <= _EDGE_TABLE_STRAY_SHARE * len(below):
        return "top"
    if above and len(below) <= _EDGE_TABLE_STRAY_SHARE * len(above):
        return "bottom"
    return None


# A line under a page-top box is part of it while it starts this many of
# its own heights below the line above, and holds no wider gap than this.
_BOX_CAPTION_LINE_GAP = 2.0
_BOX_CAPTION_MAX_WORD_GAP = 15.0


def _lines_under_box(
    t_box: tuple[float, float, float, float],
    page_bands: list[tuple[float, float]],
    rows: list[list[dict]],
) -> list[list[dict]]:
    """The lines right under a page-top box that run across a gutter of
    the columns it spans, each left to right -- see `_order_page_words`."""
    x0, _top, x1, bottom = t_box
    spanned = [(lo, hi) for lo, hi in page_bands if min(x1, hi) - max(x0, lo) > 0.5 * (hi - lo)]
    if len(spanned) < 2:
        return []
    # The box's own width as well as the columns it spans: a note under a
    # box reaching a little into the next column starts there.
    lo_x = min(x0, min(lo for lo, _ in spanned))
    hi_x = max(x1, max(hi for _, hi in spanned))
    gutters = [
        (left[1], right[0]) for left, right in zip(page_bands, page_bands[1:])
        if lo_x <= left[1] and right[0] <= hi_x
    ]
    lines: list[list[dict]] = []
    edge = bottom
    for row in sorted(rows, key=_line_top):
        words = sorted((w for w in row if w["x0"] >= lo_x - 5.0 and w["x1"] <= hi_x + 5.0), key=lambda w: w["x0"])
        if not words:
            continue
        line_top = min(w["top"] for w in words)
        if line_top < edge - 1.0:
            continue
        height = max(w.get("bottom", w["top"] + 10.0) - w["top"] for w in words)
        if line_top - edge > _BOX_CAPTION_LINE_GAP * height:
            break
        gaps = [b["x0"] - a["x1"] for a, b in zip(words, words[1:])]
        crosses = any(words[0]["x0"] < g_lo and words[-1]["x1"] > g_hi for g_lo, g_hi in gutters)
        if not crosses or (gaps and max(gaps) > _BOX_CAPTION_MAX_WORD_GAP):
            break
        lines.append(words)
        edge = max(w.get("bottom", w["top"] + 10.0) for w in words)
    return lines


# --- Rotated (non-upright) text reconstruction (MVP-14) ---------------
# See the module docstring's MVP-14 section for the full rationale.
# Rotated lines belonging to the same physical caption/label sit only a
# few points apart along the perpendicular axis (ordinary line
# spacing); a much bigger gap means the next rotated run is a
# different, unrelated element sharing the same rotation angle (e.g. a
# "33 mm" dimension callout and a "Tizanidine Hydrochloride Capsules"
# spine caption on the same 90-degree axis) that must not be
# concatenated into one reading unit or positioned as if it were one.
ROTATED_LINE_TOLERANCE = 3.0
ROTATED_BLOCK_GAP = 20.0
# A gap along a turned line wider than this many type sizes starts the
# next column.
_ROTATED_COLUMN_GAP_EM = 1.5
# A gap along a turned line ends a word when it is at least this wide
# (points, type sizes) and this many times the line's usual letter gap.
_ROTATED_WORD_GAP_MIN = 1.0
_ROTATED_WORD_GAP_EM = 0.12
_ROTATED_WORD_GAP_RATIO = 3.0


def _rotation_bucket(matrix: tuple) -> tuple[float, float, float, float]:
    """Round a character's text matrix to a stable grouping key.

    Only the (a, b, c, d) rotation/skew part matters for grouping --
    (e, f) is the character's own translation (position), which is
    exactly what should DIFFER between characters of the same rotated
    run.
    """
    a, b, c, d = matrix[0], matrix[1], matrix[2], matrix[3]
    return (round(a, 2), round(b, 2), round(c, 2), round(d, 2))


def _ordered_rotated_words(rotated_chars: list[dict]) -> list[dict]:
    """Reconstruct proper words from a page's non-upright characters,
    in a stable left-to-right order by block position.

    Returns word-dicts in the same shape ``_order_page_words`` produces
    (text/x0/top/x1/bottom), meant to be appended AFTER a page's normal
    body/table reading order -- see the MVP-14 module note on why exact
    interleaving with body paragraphs matters far less than never being
    scrambled and staying stable across two renderings of the same
    artwork.
    """
    if not rotated_chars:
        return []
    groups = _reconstruct_rotated_blocks(rotated_chars)
    # Turned blocks of one angle stay in their own reading order; angles
    # follow one another left to right.
    groups.sort(key=lambda blocks: min(w["x0"] for block in blocks for w in block))
    words: list[dict] = []
    for blocks in groups:
        for block in blocks:
            words.extend(block)
    return words


def _reconstruct_rotated_blocks(chars: list[dict]) -> list[list[list[dict]]]:
    """Group non-upright characters by rotation angle, then reconstruct
    each angle's characters into spatially-coherent blocks of words
    (see ``_words_from_rotated_group``) -- one list of blocks per angle.
    """
    by_rotation: dict[tuple, list[dict]] = {}
    for ch in chars:
        matrix = ch.get("matrix")
        if not matrix:
            continue
        by_rotation.setdefault(_rotation_bucket(matrix), []).append(dict(ch))

    groups: list[list[list[dict]]] = []
    for (a, b, c, d), group in by_rotation.items():
        blocks = _words_from_rotated_group(group, a, b, c, d)
        if blocks:
            groups.append(blocks)
    return groups


def _words_from_rotated_group(
    chars: list[dict], a: float, b: float, c: float, d: float
) -> list[list[dict]]:
    """One rotation angle's characters -> blocks of correctly-ordered
    word-dicts, in reading order along that angle.

    Projects each character's position onto the matrix's own advance
    direction (a, b) and perpendicular direction (c, d) -- see the
    MVP-14 module note -- to cluster characters into lines, order
    characters within a line, and order lines relative to each other,
    all without assuming horizontal text.

    Lines are told apart at half the type size (a fixed 3pt merged the
    3pt lines of a foil's upside-down text), cut where a column-sized gap
    opens along the line, and the pieces gathered into columns read one
    after the other: two upside-down columns side by side were read
    across, word by word ("Each Tamoxifen uncoated Citrate tablet").
    """
    for ch in chars:
        ch["_perp"] = -c * ch["x0"] + d * ch["top"]
        ch["_adv"] = a * ch["x0"] - b * ch["top"]
        ends = [a * x - b * y for x in (ch["x0"], ch["x1"]) for y in (ch["top"], ch["bottom"])]
        ch["_adv_lo"], ch["_adv_hi"] = min(ends), max(ends)

    def size(ch: dict) -> float:
        return float(ch.get("size") or 0.0) or ROTATED_LINE_TOLERANCE * 2

    ordered = sorted(chars, key=lambda ch: ch["_perp"])
    lines: list[list[dict]] = [[ordered[0]]]
    for ch in ordered[1:]:
        line_perp = sum(x["_perp"] for x in lines[-1]) / len(lines[-1])
        tolerance = min(ROTATED_LINE_TOLERANCE, 0.5 * size(ch))
        if abs(ch["_perp"] - line_perp) <= tolerance:
            lines[-1].append(ch)
        else:
            lines.append([ch])

    # Each line cut at column-sized gaps along it.
    pieces: list[list[dict]] = []
    for line in lines:
        line.sort(key=lambda ch: ch["_adv_lo"])
        piece = [line[0]]
        for ch in line[1:]:
            gap = ch["_adv_lo"] - max(x["_adv_hi"] for x in piece)
            if gap > max(2.0, _ROTATED_COLUMN_GAP_EM * size(ch)):
                pieces.append(piece)
                piece = [ch]
            else:
                piece.append(ch)
        pieces.append(piece)

    # Pieces gathered into columns: a piece joins the column whose span
    # along the line it overlaps.
    columns: list[dict] = []
    for piece in sorted(pieces, key=lambda p: sum(ch["_perp"] for ch in p) / len(p)):
        lo, hi = min(ch["_adv_lo"] for ch in piece), max(ch["_adv_hi"] for ch in piece)
        home = next((col for col in columns if lo < col["hi"] and col["lo"] < hi), None)
        if home is None:
            home = {"lo": lo, "hi": hi, "pieces": []}
            columns.append(home)
        home["lo"], home["hi"] = min(home["lo"], lo), max(home["hi"], hi)
        home["pieces"].append(piece)
    columns.sort(key=lambda col: col["lo"])

    blocks: list[list[dict]] = []
    for col in columns:
        prev_perp: Optional[float] = None
        blocks.append([])
        for piece in col["pieces"]:
            piece_perp = sum(ch["_perp"] for ch in piece) / len(piece)
            if prev_perp is not None and abs(piece_perp - prev_perp) > ROTATED_BLOCK_GAP:
                blocks.append([])
            blocks[-1].extend(_words_from_rotated_line(piece))
            prev_perp = piece_perp
    return [b for b in blocks if b]


def _words_from_rotated_line(line: list[dict]) -> list[dict]:
    """One rotated line's characters, in advance order -> word-dicts.

    Splits on whitespace characters ONLY (mirroring pdfplumber's own
    ``keep_blank_chars=False`` for horizontal text) rather than on a
    gap-distance heuristic: real rotated label text is often tightly
    and unevenly kerned per glyph pair, so the gap between two
    characters of the SAME word can be larger than the gap that
    actually separates two different words, making distance-based
    splitting unreliable. Every character carries an explicit space in
    ``page.chars`` when one is really there, which this relies on
    directly instead of guessing from spacing.
    """
    line = sorted(line, key=lambda ch: ch["_adv"])
    # The line's ordinary letter spacing, against which a word gap stands
    # out (letters of a turned word usually touch: 0pt; words 1.5pt apart).
    letter_gaps = sorted(
        max(0.0, b.get("_adv_lo", 0.0) - a.get("_adv_hi", 0.0))
        for a, b in zip(line, line[1:])
        if not a["text"].isspace() and not b["text"].isspace()
    )
    usual = letter_gaps[len(letter_gaps) // 2] if letter_gaps else 0.0
    words: list[dict] = []
    current: list[dict] = []
    for ch in line:
        if ch["text"].isspace():
            if current:
                words.append(_merge_rotated_chars(current))
                current = []
        else:
            # A space the glyph stream left out: MuPDF puts the two
            # characters in different words -- or, where MuPDF too runs
            # them together ("yourdoctoraboutyour"), a gap clearly wider
            # than the line's letter spacing.
            box, last_box = ch.get("word_box"), current[-1].get("word_box") if current else None
            gap = ch.get("_adv_lo", 0.0) - current[-1].get("_adv_hi", 0.0) if current else 0.0
            size = float(ch.get("size") or 0.0)
            wide = gap >= max(_ROTATED_WORD_GAP_MIN, _ROTATED_WORD_GAP_EM * size, _ROTATED_WORD_GAP_RATIO * usual + 0.3)
            if current and ((box is not None and last_box is not None and box != last_box) or wide):
                words.append(_merge_rotated_chars(current))
                current = []
            current.append(ch)
    if current:
        words.append(_merge_rotated_chars(current))
    return words


def _merge_rotated_chars(chars: list[dict]) -> dict:
    return {
        "text": "".join(ch["text"] for ch in chars),
        "x0": min(ch["x0"] for ch in chars),
        "top": min(ch["top"] for ch in chars),
        "x1": max(ch["x1"] for ch in chars),
        "bottom": max(ch["bottom"] for ch in chars),
    }


# A "table" whose bbox covers more than half the page is a layout
# frame (a box outline around a whole section, or the label's outer
# border), not a real table. Claiming every word on the page into one
# giant cell would flatten the page to a single (top, x0) sort and
# silently destroy the MVP-5 column-band reading order -- exactly the
# bug that produced the "double- Dizziness 11 5 When" scrambles on real
# FDA label artwork.
MAX_TABLE_AREA_FRACTION = 0.5


def _detect_page_tables(page) -> list[dict]:
    """Find ruled tables on one pdfplumber page.

    Args:
        page: A pdfplumber Page.

    Returns:
        A list of dicts with:
            "bbox":  (x0, top, x1, bottom) of the table;
            "cells": row-major grid of cell bboxes (None where a cell
                     is merged away -- pdfplumber's Table.rows[].cells
                     shape).

    Tables covering more than `MAX_TABLE_AREA_FRACTION` of the page
    are skipped -- see the comment above.
    """
    page_area = page.width * page.height
    found = page.find_tables(table_settings=_TABLE_SETTINGS_LINES)
    kept = []
    for table in found:
        x0, y0, x1, y1 = table.bbox
        if (x1 - x0) * (y1 - y0) > MAX_TABLE_AREA_FRACTION * page_area:
            num_cells = sum(1 for row in table.rows for cell in row.cells if cell is not None)
            if len(table.rows) < 3 or num_cells < 4:
                continue
        kept.append(table)

    return [
        {
            "bbox": table.bbox,
            "cells": [list(row.cells) for row in table.rows],
        }
        for table in kept
    ]


def _assign_words_to_cells(
    raw_words: list[dict],
    tables: list[dict],
    only: Optional[list[int]] = None,
    taken: Optional[set[int]] = None,
) -> dict[int, tuple[int, int, int]]:
    """Map each word whose center falls in a table to (table, row, col).
    ``only``: just these tables; ``taken``: words already in a table."""
    cell_ids: dict[int, tuple[int, int, int]] = {}
    taken = taken or set()
    for t_idx, table in enumerate(tables):
        if only is not None and t_idx not in only:
            continue
        t_x0, t_top, t_x1, t_bottom = table["bbox"]
        cell_index = [
            (r, c, cell)
            for r, row in enumerate(table["cells"])
            for c, cell in enumerate(row)
            if cell is not None
        ]
        for w_idx, w in enumerate(raw_words):
            if w_idx in cell_ids or w_idx in taken:
                continue
            mid_x = (w["x0"] + w["x1"]) / 2.0
            mid_y = (w["top"] + w["bottom"]) / 2.0
            if not (
                t_x0 - TABLE_ASSIGN_PAD <= mid_x <= t_x1 + TABLE_ASSIGN_PAD
                and t_top - TABLE_ASSIGN_PAD <= mid_y <= t_bottom + TABLE_ASSIGN_PAD
            ):
                continue
            cell_ids[w_idx] = _cell_key_for(mid_x, mid_y, cell_index, t_idx)
    return cell_ids


# How far (pt) a nested table may stick out of the table holding it.
_NESTING_TOLERANCE = 2.0


def _nesting_parents(tables: list[dict]) -> dict[int, int]:
    """Each table drawn inside another -> the smallest table holding it."""
    def area(box) -> float:
        return (box[2] - box[0]) * (box[3] - box[1])

    parents: dict[int, int] = {}
    for j, inner in enumerate(tables):
        x0, y0, x1, y1 = inner["bbox"]
        best = None
        for i, outer in enumerate(tables):
            a0, b0, a1, b1 = outer["bbox"]
            if (
                i != j
                and a0 - _NESTING_TOLERANCE <= x0 and b0 - _NESTING_TOLERANCE <= y0
                and x1 <= a1 + _NESTING_TOLERANCE and y1 <= b1 + _NESTING_TOLERANCE
                and area(outer["bbox"]) > area(inner["bbox"])
                and (best is None or area(outer["bbox"]) < area(tables[best]["bbox"]))
            ):
                best = i
        if best is not None:
            parents[j] = best
    return parents


def _nested_cell_ids(
    raw_words: list[dict],
    tables: list[dict],
    parents: dict[int, int],
    cell_ids: dict[int, tuple[int, int, int]],
    real_table_idxs: set[int],
) -> dict[int, tuple[int, int, int]]:
    """For the words of a table nested in a (real) table: which cell of
    the NESTED table each is in -- (nested table, row, col). A nested
    grid that is not a real table (a box around one paragraph) gives
    nothing, and its words are read as the outer cell's text."""
    inner_ids: dict[int, tuple[int, int, int]] = {}
    for j, parent in parents.items():
        if parent not in real_table_idxs:
            continue
        x0, top, x1, bottom = tables[j]["bbox"]
        cell_index = [
            (r, c, cell)
            for r, row in enumerate(tables[j]["cells"])
            for c, cell in enumerate(row)
            if cell is not None
        ]
        found: dict[int, tuple[int, int, int]] = {}
        for w_idx, cid in cell_ids.items():
            if cid[0] != parent or w_idx in inner_ids:
                continue
            w = raw_words[w_idx]
            mid_x = (w["x0"] + w["x1"]) / 2.0
            mid_y = (w["top"] + w["bottom"]) / 2.0
            if x0 <= mid_x <= x1 and top <= mid_y <= bottom:
                found[w_idx] = _cell_key_for(mid_x, mid_y, cell_index, j)
        if found and _looks_like_real_table(j, found) and not _cuts_through_words(tables[j], raw_words):
            inner_ids.update(found)
    return inner_ids


# A real table's whole point is that a row's RELATED data spans
# multiple columns at once (a label next to its value, an adverse-
# event name next to its N/n counts). A real observed case: a decorative
# bordered box drawn around one flowing Arabic paragraph (an "Overdose"
# section) had incidental ruling lines that `find_tables` read as a
# genuine 6-row x 3-column grid -- but EVERY row populated exactly ONE
# column (the paragraph's own line-by-line text), never two at once,
# because it was never really tabular data. Splitting it into "cells"
# then positioned each row using the TABLE's own (band, top) placement
# logic instead of the surrounding paragraph's, which -- since this
# fake table's bbox geometrically overlaps a separate, genuinely
# adjacent body paragraph -- spliced the two together into nonsense.
# Reject any detected table whose rows essentially never populate more
# than one column at once; its words fall back to ordinary body-text
# handling instead.
_MIN_MULTI_COLUMN_ROW_FRACTION = 0.3


# A real table's column edges run BETWEEN its columns, so no word ever
# crosses one. A "table" that `find_tables` infers from stray ruling
# lines over flowing text slices the lines wherever those rules happen
# to fall, and its column edges run straight THROUGH words. Reading
# such a grid cell by cell interleaves the lines: a real case, a
# leaflet's "Reporting of side effects" paragraph, came out as "This
# includes any the Yellow Play or possible Card Apple App side Scheme
# Store." -- three lines read in vertical strips.
#
# Measured on the real documents: genuine tables cross 0-5% of their
# words (ragged cell padding, a stray "=" or a wide heading), the
# inferred ones 18-61%.
_MAX_WORD_STRADDLE_FRACTION = 0.15
# Below this many words the share is noise, so the grid is left to the
# other checks.
_MIN_STRADDLE_SAMPLE_WORDS = 4
# A word has to cross an edge by more than this to count (cell edges
# often sit exactly on the ink).
_STRADDLE_TOLERANCE = 0.5


def _cuts_through_words(table: dict, raw_words: list[dict]) -> bool:
    """Whether this table's column edges run through its own words --
    the signature of a grid inferred from ruling lines over flowing
    text rather than a real table."""
    x0, top, x1, bottom = table["bbox"]
    edges = sorted(
        {
            value
            for row in table["cells"]
            for cell in row
            if cell is not None
            for value in (cell[0], cell[2])
            if x0 + 1.0 < value < x1 - 1.0
        }
    )
    if not edges:
        return False
    inside = [
        word
        for word in raw_words
        if x0 - TABLE_ASSIGN_PAD <= (word["x0"] + word["x1"]) / 2.0 <= x1 + TABLE_ASSIGN_PAD
        and top - TABLE_ASSIGN_PAD <= (word["top"] + word["bottom"]) / 2.0 <= bottom + TABLE_ASSIGN_PAD
    ]
    if len(inside) < _MIN_STRADDLE_SAMPLE_WORDS:
        return False
    # A word crosses a column edge of ITS OWN cell. Not just any column
    # edge of the table: a merged cell (a header spanning three dose
    # columns) is one wide cell, and its words cross the edges the rows
    # below have between those columns without the grid being wrong.
    cells = [cell for row in table["cells"] for cell in row if cell is not None]

    def own_cell(word: dict):
        cx = (word["x0"] + word["x1"]) / 2.0
        cy = (word["top"] + word["bottom"]) / 2.0
        holding = [c for c in cells if c[0] <= cx <= c[2] and c[1] <= cy <= c[3]]
        return min(holding, key=lambda c: (c[2] - c[0]) * (c[3] - c[1])) if holding else None

    crossing = 0
    for word in inside:
        cell = own_cell(word)
        if cell is not None and (
            word["x0"] < cell[0] - _STRADDLE_TOLERANCE or word["x1"] > cell[2] + _STRADDLE_TOLERANCE
        ):
            crossing += 1
    return crossing / len(inside) >= _MAX_WORD_STRADDLE_FRACTION


def _looks_like_real_table(
    table_idx: int, cell_ids: dict[int, tuple[int, int, int]]
) -> bool:
    row_cols: dict[int, set[int]] = {}
    for _t, r, c in cell_ids.values():
        if _t != table_idx:
            continue
        row_cols.setdefault(r, set()).add(c)
    if len(row_cols) < 2:
        return True  # too little data to second-guess find_tables here
    multi_col_rows = sum(1 for cols in row_cols.values() if len(cols) >= 2)
    return (multi_col_rows / len(row_cols)) >= _MIN_MULTI_COLUMN_ROW_FRACTION


def _cell_key_for(
    mid_x: float,
    mid_y: float,
    cell_index: list[tuple[int, int, tuple[float, float, float, float]]],
    t_idx: int,
) -> tuple[int, int, int]:
    """(table, row, col) of the cell containing (mid_x, mid_y).

    Falls back to the geometrically nearest cell when the point sits in
    a gap of the grid (e.g. a merged region whose neighbors are all
    None) so every table word still gets a deterministic key.
    """
    if not cell_index:
        return (t_idx, 0, 0)
    for r, c, (cx0, ctop, cx1, cbottom) in cell_index:
        if cx0 <= mid_x <= cx1 and ctop <= mid_y <= cbottom:
            return (t_idx, r, c)
    best: Optional[tuple[int, int, int]] = None
    best_dist = float("inf")
    for r, c, (cx0, ctop, cx1, cbottom) in cell_index:
        dx = max(cx0 - mid_x, 0.0, mid_x - cx1)
        dy = max(ctop - mid_y, 0.0, mid_y - cbottom)
        dist = math.hypot(dx, dy)
        if dist < best_dist:
            best_dist = dist
            best = (t_idx, r, c)
    assert best is not None
    return best


# A page can stack sections with GENUINELY DIFFERENT column structure
# -- a real observed case: a single-column clause, followed by a
# 2-column glossary, followed by a 3-column arbitration clause, all on
# one page. Detecting gutters once for the WHOLE page breaks down
# here: a single-column section's lines span the full content width,
# so they cross ANY candidate gutter positioned anywhere in the middle
# of the page -- including a gutter that is completely clean (zero
# crossings) within the multi-column section that actually needs it.
# The real reported failure: a genuine, unambiguous 3-column split
# (verified clean in isolation) was invisible at the page level because
# unrelated single-column sections elsewhere on the same page
# "crossed" that same x-range constantly, blowing through the
# row-crossing tolerance. `_split_into_zones` below breaks a page's
# rows at likely section-header lines (reusing the SAME header shape
# checks `core.section_diff.build_section_map` uses to find sections
# in the first place -- see `is_likely_header_line`), and
# `_order_body_units` runs column-gutter detection SEPARATELY within
# each zone, so each section's own column structure is judged only
# against its own rows.
_BARE_NUMBER = re.compile(r"^\d+(?:\.\d+)*\.?$")


def _split_into_zones(rows: list[list[dict]]) -> list[list[list[dict]]]:
    """Split rows (already in top-to-bottom order) into contiguous
    zones, starting a new zone at each row that looks like a section
    header. A page with no detected headers at all stays one zone --
    byte-for-byte the previous (single global gutter detection)
    behavior.
    """
    if not rows:
        return []
    zones: list[list[list[dict]]] = [[]]
    for row in rows:
        sorted_r = sorted(row, key=lambda w: w["x0"])
        row_text = " ".join(w["text"] for w in sorted_r)
        is_header = is_likely_header_line(row_text)
        if not is_header:
            for a, b in zip(sorted_r, sorted_r[1:]):
                if (b["x0"] - a["x1"]) >= MIN_COLUMN_GUTTER_WIDTH:
                    prefix_words = [w for w in sorted_r if w["x1"] <= a["x1"]]
                    prefix_text = " ".join(w["text"] for w in prefix_words)
                    # A bare number with more numbers beside it is a row of a
                    # table's dose headings ("20   40   80   120   160"), not
                    # section 20: the zone it opened cut a table's header in
                    # two, read as two tables.
                    dose_row = _BARE_NUMBER.match(prefix_text) and _BARE_NUMBER.match(b["text"])
                    if is_likely_header_line(prefix_text) and not dose_row:
                        is_header = True
                    break
        if zones[-1] and is_header:
            zones.append([])
        zones[-1].append(row)
    return zones


# A "column-major" reading order (read all of one band top-to-bottom
# before moving to the next) is only correct for genuinely FLOWING
# multi-column body text, where a typesetter divides the width into
# roughly EQUAL columns. A borderless "label: value" table -- no
# ruling lines, so `_detect_page_tables` (which only looks for those,
# a deliberate choice: see that function's module-docstring note on
# why a word-alignment fallback was tried and rejected) never claims
# it -- produces the SAME kind of x-gap a real column gutter does, but
# its bands are wildly UNEQUAL (a narrow label column next to a wide
# value column). Reading it column-major joins every label together,
# then every value together, instead of each row's own label-value
# pair -- a real observed case: labels like "Governing Law" /
# "Severability" / "Enforcing this Agreement" each landing before an
# unrelated LATER row's explanation instead of their own. When a
# zone's narrowest band is less than this fraction of its widest, it's
# treated as ROW-major instead (see its only caller, `_order_body_units`).
_BAND_WIDTH_BALANCE_RATIO = 0.55
# A band narrower than this (points) cannot be a column of running text.
_TEXT_COLUMN_MIN_WIDTH = 50.0


# A column of running text averages at least this many words a line.
_TEXT_COLUMN_MIN_WORDS = 4.0


def _bands_are_number_table(
    bands: list[tuple[float, float]], rows: Optional[list[list[dict]]] = None
) -> bool:
    """Three or more bands that are the columns of a TABLE, not columns
    of prose: most of them too narrow to hold running text (the number
    columns "1 | <1 | 2 | 2" under dose headings, 15pt each beside a
    label column), or holding only a few words a line (cells "80 mg" |
    "pale green oval" | "Plain on one side and debossed" -- prose
    columns run several words a line in nearly every band). Read as
    flowing columns (one band top to bottom, then the next), a table
    continued at the top of a page read every label first, then every
    value, column by column. Such a zone is read line by line, with
    table rows read cell by cell (`_read_lines`)."""
    if len(bands) < 3:
        return False
    # Per band: words, lines, and lines that open with a list bullet. A
    # column of bulleted items ("• ritonavir" / "• rifampin" side by side
    # with two more such columns) is a LIST set in columns -- short, but
    # read column by column like text, and never a table.
    words = [0] * len(bands)
    lines = [0] * len(bands)
    bulleted = [0] * len(bands)
    for row in rows or []:
        in_band: dict[int, list[dict]] = {}
        for w in row:
            in_band.setdefault(_band_index_for((w["x0"] + w["x1"]) / 2.0, bands), []).append(w)
        for b, band_words in in_band.items():
            words[b] += len(band_words)
            lines[b] += 1
            first = min(band_words, key=lambda w: w["x0"])["text"]
            if first in _LIST_BULLETS:
                bulleted[b] += 1
    lists = {b for b in range(len(bands)) if lines[b] and bulleted[b] * 2 >= lines[b]}
    if lists:
        return False
    narrow = sum(1 for lo, hi in bands if hi - lo < _TEXT_COLUMN_MIN_WIDTH)
    if narrow * 2 > len(bands):
        return True
    if not rows:
        return False
    prose = sum(1 for b in range(len(bands)) if lines[b] and words[b] / lines[b] >= _TEXT_COLUMN_MIN_WORDS)
    return prose * 2 <= len(bands)


# Words that open an item of a bulleted list.
_LIST_BULLETS = frozenset({"•", "o", "○", "ο", "◦", "▪", "■", "●", "-", "–", "—", "✓", "➢", "►", "*"})


def _bands_are_column_flow(bands: list[tuple[float, float]]) -> bool:
    """See the module note above for the label/value-table rationale.

    MVP-21 addendum: a real reported oversized proof sheet (four
    genuine, independently-gutter-detected text columns -- verified
    clean via the same row-crossing-count algorithm TEST 15 already
    locks in) had one column only ~70pt wide (a hanging bullet-marker
    strip in a list-heavy clause) next to others ~300-470pt wide,
    failing the width-balance ratio below and collapsing the WHOLE
    zone to row-major -- which then read all four columns
    interleaved word-by-word at whatever height their lines happened
    to coincide, scrambling four unrelated clauses together into
    nonsense. A borderless "label: value" table -- the case this
    ratio check exists for -- inherently has exactly TWO columns: one
    narrow label, one wide value; a genuine 3rd or 4th independently-
    detected column essentially never arises from that shape. So once
    3+ bands are found, skip the width-ratio check entirely and
    always treat the zone as column-flow -- narrow individual bands
    (bullet strips, a numeric ID column) stay real, ordered columns
    instead of being used to indict the whole zone.
    """
    if len(bands) <= 1:
        return False
    if len(bands) >= 3:
        return True
    widths = [hi - lo for lo, hi in bands]
    return min(widths) >= _BAND_WIDTH_BALANCE_RATIO * max(widths)


# A borderless table's VALUE cell can ITSELF wrap across two further
# sub-columns of independent text (a real observed case: a credit-card
# agreement's Fees table has a narrow "Late Fee" / "Returned Payment
# Fee" / ... label column, and a wide value region that -- for SOME
# rows only -- splits into a "for Purchases/Balance Transfers" run and
# a parallel "for Cash Advances" run, each wrapping onto its OWN
# following line(s)). That inner split is invisible to the zone-wide
# gutter sweep above for the same reason `_split_into_zones` exists
# one level up: most OTHER rows in the SAME zone are single, full-
# width paragraphs with no such split (e.g. "Permitted Uses",
# "Authorized Users"), so their lines "cross" that narrow interior
# x-range constantly and the row-crossing-count tolerance correctly
# rejects it as a real ZONE-WIDE gutter -- even though it is a
# perfectly real, locally-consistent gutter for the handful of rows
# that actually have it.
#
# Reading the whole zone row-major (one physical line at a time,
# left-to-right) breaks badly here: when the two sub-columns wrap to
# a DIFFERENT number of lines for the same record (verified against
# the real reported document), a later physical line ends up mixing
# the TAIL of one record's sub-column with the HEAD of the next
# record's label -- e.g. "We will charge you a Returned Check Fee...
# six billing periods. Otherwise, the fee is $35..." splicing two
# unrelated fee explanations together into nonsense.
#
# Fix: partition the zone's rows into per-record groups at each row
# carrying a word in the LABEL band (`bands[0]`) -- the first physical
# line of every record always has one; every wrapped continuation
# line never does, a purely geometric signal requiring no knowledge of
# what a "label" means. Within one record, look for a genuine internal
# split with `_consistent_internal_gutter_x` (NOT the general
# `_detect_column_bands` row-crossing-count sweep -- that sweep's
# tolerance floors at "at least 1 row may cross" no matter how few
# rows there are, which is fine for a whole page/zone but statistically
# meaningless for the 2-3 lines a single record typically has: nearly
# every ordinary word-to-word gap in a 2-row cell satisfies "at most 1
# of 2 rows crosses it", so it was fragmenting a real record into up to
# MAX_COLUMN_BANDS=8 nonsense slivers, verified directly against the
# real reported document). If a real, per-line-persistent internal
# split is found, read it column-major for THIS record only (one
# sub-column fully, top to bottom, then the next); if not (the common
# case -- a single flowing paragraph), read it as its own natural row
# order. The records themselves stay in top-to-bottom order regardless.
def _consistent_internal_gutter_x(
    value_rows: list[list[dict]], min_gutter: float
) -> Optional[float]:
    """An x-position where EVERY multi-word row has its OWN widest
    inter-word gap (of at least ``min_gutter``), and all those rows'
    gap intervals share a common overlap -- i.e. a split that is
    persistent on literally every line of this one record, not just
    "most" rows. Deliberately stricter than `_detect_column_bands`
    (see the note above): a record only ever has a handful of lines,
    too few for a crossing-count TOLERANCE to mean anything, so this
    requires unanimous agreement instead. Returns None (no split) if
    fewer than 2 usable rows, any row lacks a wide-enough gap, or the
    rows disagree on where it is.
    """
    candidate_rows = [r for r in value_rows if len(r) >= 2]
    if len(candidate_rows) < 2:
        return None
    overlap_lo, overlap_hi = None, None
    for row in candidate_rows:
        ordered = sorted(row, key=lambda w: w["x0"])
        best_gap: Optional[tuple[float, float, float]] = None
        for a, b in zip(ordered, ordered[1:]):
            gap = b["x0"] - a["x1"]
            if gap >= min_gutter and (best_gap is None or gap > best_gap[0]):
                best_gap = (gap, a["x1"], b["x0"])
        if best_gap is None:
            return None
        _, lo, hi = best_gap
        overlap_lo = lo if overlap_lo is None else max(overlap_lo, lo)
        overlap_hi = hi if overlap_hi is None else min(overlap_hi, hi)
        if overlap_lo >= overlap_hi:
            return None
    return (overlap_lo + overlap_hi) / 2.0


def _order_label_value_zone(
    zone_rows: list[list[dict]],
    bands: list[tuple[float, float]],
    *,
    rtl: bool = False,
) -> list[list[dict]]:
    # `bands` is always in ascending x0 (left-to-right, spatial) order.
    # The LABEL column is the first one read, which for RTL is the
    # RIGHTMOST band (bands[-1]), not the leftmost -- see the matching
    # RTL note in `_order_body_units`'s column-flow branch.
    label_band = bands[-1] if rtl else bands[0]

    # A word before the label column (left of it; right of it in RTL),
    # outside every band, is read with the label, not after the values:
    # "New code | Revised: February 2026", where the bands began at "code",
    # read "code New Revised: ...".
    def in_label(mid_x: float) -> bool:
        return mid_x >= label_band[0] if rtl else mid_x <= label_band[1]

    groups: list[list[list[dict]]] = [[]]
    for row in zone_rows:
        has_label = any(in_label((w["x0"] + w["x1"]) / 2.0) for w in row)
        if has_label and groups[-1]:
            groups.append([])
        groups[-1].append(row)

    ordered_lines: list[list[dict]] = []
    for group_rows in groups:
        label_words: list[dict] = []
        value_words: list[dict] = []
        for row in group_rows:
            for w in row:
                mid_x = (w["x0"] + w["x1"]) / 2.0
                if in_label(mid_x):
                    label_words.append(w)
                else:
                    value_words.append(w)

        for line in _group_into_lines(label_words):
            ordered_lines.append(_sort_line_words(line, rtl=rtl))

        if not value_words:
            continue

        value_rows = sorted(_group_into_lines(value_words), key=_line_top)
        split_x = _consistent_internal_gutter_x(
            value_rows, _adaptive_min_gutter(value_rows)
        )
        if split_x is not None:
            left_words = [w for w in value_words if (w["x0"] + w["x1"]) / 2.0 <= split_x]
            right_words = [w for w in value_words if (w["x0"] + w["x1"]) / 2.0 > split_x]
            # RTL reads the RIGHT sub-column first.
            sub_columns = (right_words, left_words) if rtl else (left_words, right_words)
            for words_in_band in sub_columns:
                if not words_in_band:
                    continue
                for line in _group_into_lines(words_in_band):
                    ordered_lines.append(_sort_line_words(line, rtl=rtl))
        else:
            for line in value_rows:
                ordered_lines.append(_sort_line_words(line, rtl=rtl))

    return ordered_lines


def _is_robust_page_columns(
    bands: list[tuple[float, float]], content_x0: float, content_x1: float
) -> bool:
    """True if `bands` represents genuine, multi-column layout covering the page.

    Rejects dummy slivers (e.g. TEST 15 where 3 words had 225pt gaps between them)
    so mixed-layout pages still split into zones.
    """
    if len(bands) < 2:
        return False
    total_content_width = max(1.0, content_x1 - content_x0)
    total_band_width = sum(hi - lo for lo, hi in bands)
    if total_band_width / total_content_width < 0.50:
        return False
    min_width = min(hi - lo for lo, hi in bands)
    if min_width < 25.0:
        return False
    return _bands_are_column_flow(bands)


def _detect_page_panels(
    rows: list[list[dict]],
    content_x0: float,
    content_x1: float,
) -> Optional[list[tuple[float, float]]]:
    """Detect high-level physical panels on oversized multi-page imposition proof flats.

    An oversized proof flat (e.g. 11x17, A3, or 2-up press proof with content width >= 750pt)
    places multiple full document pages side-by-side separated by a wide trim/fold gutter.
    Returns the panel bands when genuine multi-page panels are detected, else None.
    """
    if (content_x1 - content_x0) < 750.0:
        return None
    raw_bands = _detect_column_bands(rows, content_x0, content_x1, min_gutter=20.0)
    if len(raw_bands) < 2:
        return None
    panels: list[list[float]] = []
    for lo, hi in raw_bands:
        w = hi - lo
        if not panels:
            panels.append([lo, hi])
        elif w < 200.0 or (panels[-1][1] - panels[-1][0]) < 200.0:
            panels[-1][1] = hi
        else:
            panels.append([lo, hi])
    result = [(lo, hi) for lo, hi in panels if (hi - lo) >= 200.0]
    if len(result) >= 2:
        return result
def _merge_narrow_column_bands(
    bands: list[tuple[float, float]],
    min_width: float = 25.0,
) -> list[tuple[float, float]]:
    """Merge bands narrower than min_width into adjacent bands when adjacent to a wider column."""
    if len(bands) <= 1:
        return bands
    merged = list(bands)
    changed = True
    while changed and len(merged) > 1:
        changed = False
        for i, (lo, hi) in enumerate(merged):
            if (hi - lo) < min_width:
                has_wider_neighbor = (i > 0 and (merged[i - 1][1] - merged[i - 1][0]) >= min_width) or (
                    i < len(merged) - 1 and (merged[i + 1][1] - merged[i + 1][0]) >= min_width
                )
                if not has_wider_neighbor:
                    continue
                if i > 0 and i < len(merged) - 1:
                    prev_dist = lo - merged[i - 1][1]
                    next_dist = merged[i + 1][0] - hi
                    if prev_dist <= next_dist:
                        merged[i - 1] = (merged[i - 1][0], max(merged[i - 1][1], hi))
                        merged.pop(i)
                    else:
                        merged[i + 1] = (min(merged[i + 1][0], lo), merged[i + 1][1])
                        merged.pop(i)
                elif i > 0:
                    merged[i - 1] = (merged[i - 1][0], max(merged[i - 1][1], hi))
                    merged.pop(i)
                else:
                    merged[i + 1] = (min(merged[i + 1][0], lo), merged[i + 1][1])
                    merged.pop(i)
                changed = True
                break
    return merged


# A strip narrower than a text column averaging fewer words a line than
# this, mostly numbering labels, holds labels hanging beside a column
# (section numbers, line numbers), not a column of its own.
_LABEL_STRIP_MAX_WORDS = 2.0
_LABEL_STRIP_MIN_LABEL_SHARE = 0.6
_NUMBERING_LABEL = re.compile(r"^\(?(?:\d{1,3}(?:\.\d{1,3})*|[ivxlc]{1,5}|[a-zA-Z])[.):]?$")


def _merge_label_strips(
    bands: list[tuple[float, float]], rows: list[list[dict]]
) -> list[tuple[float, float]]:
    """``bands`` with each strip of hanging labels merged into the column
    it labels, the one to its right: a narrow strip, few words a line,
    most of them numbering labels ("1", "2.3", "(a)", "iv.").

    A Contents list sets "1", "2" ... "7" at x=36 and their headings at
    x=54: the 13pt gap was taken for a gutter, the numbers became a
    column of their own and were read first, all together ("FULL 1 2 3 4
    5 6 7 Reference PRESCRIBING ..."), leaving every heading without its
    number. A narrow strip of anything else (a signature block's
    "Operator/Dev.", an artwork code) is left as it was."""
    if len(bands) < 3:
        return bands  # two bands: a real page of columns, or a label/value table (handled elsewhere)
    merged = list(bands)
    changed = True
    while changed and len(merged) >= 3:
        changed = False
        lines = [0] * len(merged)
        words: list[list[str]] = [[] for _ in merged]
        for row in rows:
            per_band: dict[int, list[str]] = {}
            for w in row:
                per_band.setdefault(_band_index_for((w["x0"] + w["x1"]) / 2.0, merged), []).append(w["text"])
            for b, texts in per_band.items():
                lines[b] += 1
                words[b].extend(texts)
        for i, (lo, hi) in enumerate(merged[:-1]):
            if hi - lo >= _TEXT_COLUMN_MIN_WIDTH or not lines[i] or len(words[i]) / lines[i] >= _LABEL_STRIP_MAX_WORDS:
                continue
            labels = sum(1 for text in words[i] if _NUMBERING_LABEL.match(text))
            if labels < _LABEL_STRIP_MIN_LABEL_SHARE * len(words[i]):
                continue
            merged[i + 1] = (min(lo, merged[i + 1][0]), merged[i + 1][1])
            merged.pop(i)
            changed = True
            break
    return merged


def _find_gutter_crossing_hlines(
    page: Any,
    bands: list[tuple[float, float]],
    body_words: list[dict],
) -> list[tuple[float, int, int]]:
    """Find horizontal ruling lines or rects that span across at least one column gutter.

    Returns list of (y_cut, first_band_idx, last_band_idx) sorted by y_cut.
    """
    if not page or len(bands) < 2:
        return []
    candidates: list[tuple[float, float, float]] = []
    for line in getattr(page, "lines", []):
        top, bottom = float(line["top"]), float(line["bottom"])
        x0, x1 = float(line["x0"]), float(line["x1"])
        if abs(bottom - top) <= 2.5 and (x1 - x0) >= 30.0:
            candidates.append(((top + bottom) / 2.0, min(x0, x1), max(x0, x1)))
    for rect in getattr(page, "rects", []):
        top, bottom = float(rect["top"]), float(rect["bottom"])
        x0, x1 = float(rect["x0"]), float(rect["x1"])
        height = bottom - top
        width = x1 - x0
        if 0.2 <= height <= 3.0 and width >= 30.0:
            candidates.append(((top + bottom) / 2.0, min(x0, x1), max(x0, x1)))
    if not candidates:
        return []
    valid_cuts: list[tuple[float, int, int]] = []
    for y_mid, lx0, lx1 in candidates:
        covered_bands = [i for i, (bx0, bx1) in enumerate(bands) if not (lx1 < bx0 or lx0 > bx1)]
        if len(covered_bands) < 2:
            continue
        first_b, last_b = min(covered_bands), max(covered_bands)
        w_first = bands[first_b][1] - bands[first_b][0]
        w_last = bands[last_b][1] - bands[last_b][0]
        if lx0 > (bands[first_b][0] + 0.35 * w_first) or lx1 < (bands[last_b][1] - 0.35 * w_last):
            continue
        crosses_all = True
        for b in range(first_b, last_b):
            gutter_x0 = bands[b][1]
            gutter_x1 = bands[b + 1][0]
            if not (lx0 <= gutter_x0 + 2.0 and lx1 >= gutter_x1 - 2.0):
                crosses_all = False
                break
        if not crosses_all:
            continue
        words_above = [
            w for w in body_words
            if any(bands[b][0] <= (w["x0"] + w["x1"]) / 2.0 <= bands[b][1] for b in range(first_b, last_b + 1))
            and w["bottom"] < y_mid and y_mid - w["bottom"] < 600.0
        ]
        words_below = [
            w for w in body_words
            if any(bands[b][0] <= (w["x0"] + w["x1"]) / 2.0 <= bands[b][1] for b in range(first_b, last_b + 1))
            and w["top"] > y_mid and w["top"] - y_mid < 600.0
        ]
        if len(words_above) >= 15 and len(words_below) >= 15:
            if not any(abs(y_mid - vc[0]) < 3.0 and vc[1] == first_b and vc[2] == last_b for vc in valid_cuts):
                valid_cuts.append((y_mid, first_b, last_b))
    valid_cuts.sort(key=lambda c: c[0])
    return valid_cuts


def _generate_column_streams(
    page_bands: list[tuple[float, float]],
    h_cuts: list[tuple[float, int, int]],
    *,
    rtl: bool = False,
) -> list[tuple[tuple[float, float], float, float, int]]:
    """Generate ordered stream descriptors: (col_band, y_min, y_max, col_idx)."""
    n_bands = len(page_bands)
    if not h_cuts:
        cols = list(reversed(range(n_bands))) if rtl else list(range(n_bands))
        return [(page_bands[c], -1e9, 1e9, c) for c in cols]
    band_cuts: dict[int, list[float]] = {b: [] for b in range(n_bands)}
    for y_cut, fb, lb in h_cuts:
        for b in range(fb, lb + 1):
            band_cuts[b].append(y_cut)
    groups: list[list[int]] = []
    curr_group: list[int] = [0]
    for b in range(1, n_bands):
        if band_cuts[b] and band_cuts[b] == band_cuts[curr_group[-1]]:
            curr_group.append(b)
        else:
            groups.append(curr_group)
            curr_group = [b]
    groups.append(curr_group)
    streams: list[tuple[tuple[float, float], float, float, int]] = []
    ordered_groups = list(reversed(groups)) if rtl else groups
    for grp in ordered_groups:
        cuts = sorted(set(band_cuts[grp[0]]))
        if not cuts:
            cols = list(reversed(grp)) if rtl else grp
            for c in cols:
                streams.append((page_bands[c], -1e9, 1e9, c))
        else:
            y_bounds = [-1e9] + cuts + [1e9]
            for s in range(len(y_bounds) - 1):
                y_lo, y_hi = y_bounds[s], y_bounds[s + 1]
                cols = list(reversed(grp)) if rtl else grp
                for c in cols:
                    streams.append((page_bands[c], y_lo, y_hi, c))
    return streams


def _order_body_units(
    body_words: list[dict],
    content_x0: float,
    content_x1: float,
    *,
    rtl: bool = False,
    page: Optional[Any] = None,
) -> tuple[list[tuple[int, float, list[dict]]], list[tuple[float, float]]]:
    """Non-table words -> (band_index, line_top, words) reading units.

    Runs the MVP-5 column-band heuristic on body words only, so table
    cell gutters can no longer confuse page-level gutter detection --
    and, since MVP-20, runs it SEPARATELY per zone (see
    `_split_into_zones` above) so sections with different column
    structure on the same page can't confuse each other's gutter
    detection either. Returns (units, bands); every unit carries the
    index of the (zone-local, but globally unique across the returned
    `bands` list) band it was bucketed into, and the average `top` of
    its line as a vertical sort key.

    Hierarchical column & zone processing: if the whole page has 2+ robust,
    flowing page-level columns (e.g. an oversized multi-column proof sheet,
    or a page combining an insert column with main agreement sections),
    each page-level column is processed as its own vertical stream. Within
    that column, words split into zones at column-local headers so borderless
    label/value tables inside that column are properly recognized and read
    row-major.
    """
    if not body_words:
        return [], [(content_x0, content_x1)]

    rows = sorted(_group_into_lines(body_words), key=_line_top)
    page_bands = _detect_column_bands(
        rows, content_x0, content_x1, min_gutter=_adaptive_min_gutter(rows)
    )
    page_bands = _merge_narrow_column_bands(page_bands, min_width=25.0)
    page_bands = _merge_label_strips(page_bands, rows)
    is_page_cols = len(page_bands) >= 2 and _is_robust_page_columns(page_bands, content_x0, content_x1)
    if not is_page_cols:
        page_panels = _detect_page_panels(rows, content_x0, content_x1)
        if page_panels is not None:
            page_bands = page_panels
            is_page_cols = True

    if is_page_cols:
        band_words_list: list[list[dict]] = [[] for _ in page_bands]
        for w in body_words:
            mid_x = (w["x0"] + w["x1"]) / 2.0
            band_words_list[_band_index_for(mid_x, page_bands)].append(w)

        reverse_columns = rtl and _should_reverse_rtl_columns(band_words_list)
        h_cuts = _find_gutter_crossing_hlines(page, page_bands, body_words)
        streams = _generate_column_streams(page_bands, h_cuts, rtl=reverse_columns)

        all_units: list[tuple[int, float, list[dict]]] = []
        all_bands: list[tuple[float, float]] = []

        hrules = _horizontal_rules(page)

        def emit_stream(col_words: list[dict]) -> None:
            col_rows = sorted(_group_column_lines(col_words), key=_line_top)
            col_zones = _split_into_zones(col_rows)
            for zone_rows, peeled_bands in _peel_spanning_rows(col_zones):
                zone_words = [w for row in zone_rows for w in row]
                zone_x0 = min(w["x0"] for w in zone_words)
                zone_x1 = max(w["x1"] for w in zone_words)
                band_offset = len(all_bands)
                if len(zone_rows) < 3:
                    all_bands.append((zone_x0, zone_x1))
                    for top, words in _read_lines(zone_rows, rtl=rtl):
                        all_units.append((band_offset, top, words))
                    continue
                zone_bands = peeled_bands or _detect_column_bands(
                    zone_rows, zone_x0, zone_x1, min_gutter=_adaptive_min_gutter(zone_rows)
                )
                band_offset = len(all_bands)
                if _bands_are_number_table(zone_bands, zone_rows):
                    all_bands.append((zone_x0, zone_x1))
                    for top, words in _read_lines(zone_rows, rtl=rtl):
                        all_units.append((band_offset, top, words))
                elif len(zone_bands) >= 2 and not _bands_are_column_flow(zone_bands):
                    all_bands.append((zone_x0, zone_x1))
                    for line_idx, line in enumerate(
                        _order_label_value_zone(zone_rows, zone_bands, rtl=rtl)
                    ):
                        all_units.append((band_offset, float(line_idx), line))
                elif len(zone_bands) >= 2 and _bands_are_column_flow(zone_bands):
                    n_b = len(zone_bands)
                    band_words = [[] for _ in zone_bands]
                    for w in zone_words:
                        mid_x = (w["x0"] + w["x1"]) / 2.0
                        band_words[_band_index_for(mid_x, zone_bands)].append(w)
                    reverse_cols = rtl and _should_reverse_rtl_columns(band_words)
                    all_bands.extend(reversed(zone_bands) if reverse_cols else zone_bands)
                    for raw_idx, words_in_band in enumerate(band_words):
                        if not words_in_band:
                            continue
                        out_idx = (n_b - 1 - raw_idx) if reverse_cols else raw_idx
                        for top, words in _read_lines(_group_column_lines(words_in_band), rtl=rtl):
                            all_units.append((band_offset + out_idx, top, words))
                else:
                    all_bands.append((zone_x0, zone_x1))
                    for top, words in _read_lines(zone_rows, rtl=rtl):
                        all_units.append((band_offset, top, words))

        for col_band, y_lo, y_hi, col_idx in streams:
            col_words = [
                w for w in body_words
                if _band_index_for((w["x0"] + w["x1"]) / 2.0, page_bands) == col_idx
                and y_lo <= (w["top"] + w["bottom"]) / 2.0 < y_hi
            ]
            if not col_words:
                continue
            segments = _sub_column_segments(
                sorted(_group_column_lines(col_words), key=_line_top), hrules
            )
            if segments is None:
                emit_stream(col_words)  # no two-column block: exactly as before
                continue
            for segment_words, gutter in segments:
                if gutter is None:
                    emit_stream(segment_words)
                    continue
                split_x = (gutter[0] + gutter[1]) / 2.0
                left = [w for w in segment_words if (w["x0"] + w["x1"]) / 2.0 < split_x]
                right = [w for w in segment_words if (w["x0"] + w["x1"]) / 2.0 >= split_x]
                parts = [left, right]
                if rtl and _should_reverse_rtl_columns(parts):
                    parts.reverse()
                for part in parts:
                    if part:
                        emit_stream(part)

        return all_units, all_bands

    zones = _split_into_zones(rows)
    all_units: list[tuple[int, float, list[dict]]] = []
    all_bands: list[tuple[float, float]] = []
    for zone_rows, peeled_bands in _peel_spanning_rows(zones):
        zone_words = [w for row in zone_rows for w in row]
        zone_x0 = min(w["x0"] for w in zone_words)
        zone_x1 = max(w["x1"] for w in zone_words)
        band_offset = len(all_bands)
        if len(zone_rows) < 3:
            all_bands.append((zone_x0, zone_x1))
            for top, words in _read_lines(zone_rows, rtl=rtl):
                all_units.append((band_offset, top, words))
            continue
        zone_bands = peeled_bands or _detect_column_bands(
            zone_rows, zone_x0, zone_x1, min_gutter=_adaptive_min_gutter(zone_rows)
        )
        band_offset = len(all_bands)

        if _bands_are_number_table(zone_bands, zone_rows):
            all_bands.append((zone_x0, zone_x1))
            for top, words in _read_lines(zone_rows, rtl=rtl):
                all_units.append((band_offset, top, words))
            continue

        if len(zone_bands) >= 2 and not _bands_are_column_flow(zone_bands):
            all_bands.append((zone_x0, zone_x1))
            for line_idx, line in enumerate(
                _order_label_value_zone(zone_rows, zone_bands, rtl=rtl)
            ):
                all_units.append((band_offset, float(line_idx), line))
            continue

        if len(zone_bands) <= 1:
            all_bands.append((zone_x0, zone_x1))
            for top, words in _read_lines(zone_rows, rtl=rtl):
                all_units.append((band_offset, top, words))
            continue

        n_bands = len(zone_bands)
        band_words = [[] for _ in zone_bands]
        for w in zone_words:
            mid_x = (w["x0"] + w["x1"]) / 2.0
            band_words[_band_index_for(mid_x, zone_bands)].append(w)

        reverse_columns = rtl and _should_reverse_rtl_columns(band_words)
        all_bands.extend(reversed(zone_bands) if reverse_columns else zone_bands)

        for raw_idx, words_in_band in enumerate(band_words):
            if not words_in_band:
                continue
            out_idx = (n_bands - 1 - raw_idx) if reverse_columns else raw_idx
            for top, words in _read_lines(_group_column_lines(words_in_band), rtl=rtl):
                all_units.append((band_offset + out_idx, top, words))

    return all_units, all_bands


# Two columns under a line that spans them -- a foil's product name over
# its two text columns, an address line under them -- found no gutter: the
# zone's few rows allow one crossing line, not two, and both columns were
# read across, line by line ("Each Tamoxifen uncoated Citrate tablet IP
# contains DRUG It is dangerous"). Up to this many lines are set aside at
# each end of such a zone, when what is left still holds this many lines
# in two or more columns no line crosses.
_SPANNING_PEEL_MAX = 3
_SPANNING_PEEL_MIN_INNER = 6
# Share of the remaining lines with text on both sides of a gutter.
_SPANNING_PEEL_MIN_WITNESS = 0.6


def _zone_bands(zone_rows: list[list[dict]], tolerance_fraction: float = COLUMN_GUTTER_ROW_TOLERANCE_FRACTION) -> list[tuple[float, float]]:
    words = [w for row in zone_rows for w in row]
    return _detect_column_bands(
        zone_rows, min(w["x0"] for w in words), max(w["x1"] for w in words),
        min_gutter=_adaptive_min_gutter(zone_rows), tolerance_fraction=tolerance_fraction,
    )


# A narrow gutter's right column: lines opening after a gap this many word
# spaces wide, starting within this many points of each other, in this
# share of the lines; a strip at least this many word spaces wide clear
# in every line; each side this wide; type sizes this different.
_ALIGNED_EDGE_GAP_WORDS = 2.0
_ALIGNED_EDGE_SPREAD = 2.0
_ALIGNED_EDGE_MIN_SHARE = 0.7
_ALIGNED_EDGE_MIN_GAP_WORDS = 1.5
_ALIGNED_EDGE_MIN_SIDE = 30.0
_ALIGNED_EDGE_SIZE_CONTRAST = 0.1


def _aligned_edge_bands(rows: list[list[dict]]) -> Optional[list[tuple[float, float]]]:
    """Two columns whose gutter is no wider than a few word spaces (2pt on
    one foil, against 0.9pt word spacing), recognised by everything else
    that marks a column: the right column's lines all start at one x, no
    line crosses the strip before it, and the two sides are set in
    different sizes of type. None when any of that is missing."""
    if len(rows) < _SPANNING_PEEL_MIN_INNER:
        return None
    word_gap = _typical_word_gap(rows) or 1.0
    # Per line, the words that open a run after a gap wider than a word space.
    starts: list[list[float]] = []
    for row in rows:
        ws = sorted(row, key=lambda w: w["x0"])
        starts.append([
            b["x0"] for a, b in zip(ws, ws[1:]) if b["x0"] - a["x1"] >= _ALIGNED_EDGE_GAP_WORDS * word_gap
        ])
    best_lo, best_count = None, 0
    for x in sorted({x for xs in starts for x in xs}):
        count = sum(1 for xs in starts if any(x <= v <= x + _ALIGNED_EDGE_SPREAD for v in xs))
        if count > best_count:
            best_lo, best_count = x, count
    if best_lo is None or best_count < _ALIGNED_EDGE_MIN_SHARE * len(rows):
        return None
    left = [w for row in rows for w in row if w["x0"] < best_lo - 0.01]
    right = [w for row in rows for w in row if w["x0"] >= best_lo - 0.01]
    if not left or not right:
        return None
    g_lo = max(w["x1"] for w in left)
    if best_lo - g_lo < _ALIGNED_EDGE_MIN_GAP_WORDS * word_gap:
        return None  # some line runs on into the strip
    x0 = min(w["x0"] for w in left)
    x1 = max(w["x1"] for w in right)
    if min(g_lo - x0, x1 - best_lo) < _ALIGNED_EDGE_MIN_SIDE:
        return None

    def median_height(ws: list[dict]) -> float:
        hs = sorted(w.get("bottom", 0.0) - w.get("top", 0.0) for w in ws)
        return hs[len(hs) // 2]

    lh, rh = median_height(left), median_height(right)
    if abs(lh - rh) < _ALIGNED_EDGE_SIZE_CONTRAST * max(lh, rh):
        return None
    return [(x0, g_lo), (best_lo, x1)]


def _peel_spanning_rows(
    zones: list[list[list[dict]]],
) -> list[tuple[list[list[dict]], Optional[list[tuple[float, float]]]]]:
    """Each zone with None (its columns found as usual), or -- when it
    reads as one band only because a line or two at its top or bottom
    cross its columns' gutter, or because that gutter is too narrow for a
    width test alone -- split into those lines, the columns between them
    (with their bands) and the lines after."""
    result: list[tuple[list[list[dict]], Optional[list[tuple[float, float]]]]] = []
    for zone_rows in zones:
        n = len(zone_rows)
        if n < _SPANNING_PEEL_MIN_INNER or len(_zone_bands(zone_rows)) >= 2:
            result.append((zone_rows, None))
            continue
        best: Optional[tuple[int, int, list[tuple[float, float]]]] = None
        for peeled in range(0, 2 * _SPANNING_PEEL_MAX + 1):
            for top in range(max(0, peeled - _SPANNING_PEEL_MAX), min(peeled, _SPANNING_PEEL_MAX) + 1):
                bottom = peeled - top
                inner = zone_rows[top:n - bottom]
                if len(inner) < _SPANNING_PEEL_MIN_INNER:
                    continue
                bands = _zone_bands(inner, tolerance_fraction=0.0)
                if len(bands) < 2 or not _bands_are_column_flow(bands) or _bands_are_number_table(bands, inner):
                    bands = _aligned_edge_bands(inner)
                    if bands is None:
                        continue
                gutters = [(a[1], b[0]) for a, b in zip(bands, bands[1:])]
                witnesses = sum(
                    1 for row in inner
                    if all(
                        any(w["x1"] <= g0 + 0.01 for w in row) and any(w["x0"] >= g1 - 0.01 for w in row)
                        for g0, g1 in gutters
                    )
                )
                if witnesses < _SPANNING_PEEL_MIN_WITNESS * len(inner):
                    continue
                spanning = zone_rows[:top] + zone_rows[n - bottom:]
                if all(
                    any(w["x0"] < g1 and w["x1"] > g0 for w in row for g0, g1 in gutters)
                    for row in spanning
                ):
                    best = (top, bottom, bands)
                    break
            if best:
                break
        if best is None:
            result.append((zone_rows, None))
            continue
        top, bottom, bands = best
        for part, part_bands in (
            (zone_rows[:top], None), (zone_rows[top:n - bottom], bands), (zone_rows[n - bottom:], None)
        ):
            if part:
                result.append((part, part_bands))
    return result


# A two-column block inside ONE page column -- the Highlights box of a
# prescribing-information outsert, or its Contents list, set as two narrow
# columns at the top of the sheet's first column. Its gutter can be under
# 5pt (4.9pt on one outsert), below what a whole page or a heading-sized
# zone accepts as a gutter, so both sub-columns were read line by line
# together and the comparison paired Highlights text with whatever came
# nearest. Recognised only when the evidence is overwhelming:
_SUBCOLUMN_MIN_ROWS = 25          # lines in a row with the same strip empty
_SUBCOLUMN_MIN_GAP = 4.0          # points, exact
_SUBCOLUMN_GAP_PER_WORD_GAP = 2.5  # and this many times the word spacing
_SUBCOLUMN_MIN_SIDE = 60.0        # each sub-column at least this wide
_SUBCOLUMN_MIN_WITNESS = 0.6      # share of those lines with text on BOTH sides


def _horizontal_rules(page: Optional[Any]) -> list[tuple[float, float, float]]:
    """(y, x0, x1) of the page's horizontal ruling lines (thin lines and
    hairline rectangles)."""
    if page is None:
        return []
    rules = []
    for line in getattr(page, "lines", []) or []:
        if abs(float(line["bottom"]) - float(line["top"])) <= 2.5:
            rules.append(((float(line["top"]) + float(line["bottom"])) / 2.0, float(line["x0"]), float(line["x1"])))
    for rect in getattr(page, "rects", []) or []:
        if float(rect["bottom"]) - float(rect["top"]) <= 3.0:
            rules.append(((float(rect["top"]) + float(rect["bottom"])) / 2.0, float(rect["x0"]), float(rect["x1"])))
    return rules


def _sub_column_segments(
    rows: list[list[dict]],
    hrules: Optional[list[tuple[float, float, float]]] = None,
) -> Optional[list[tuple[list[dict], Optional[tuple[float, float]]]]]:
    """Split one page column's lines (top to bottom) into vertical
    segments: two-column blocks, each with its gutter, and ordinary text
    (gutter None). Returns None when there is no two-column block, so the
    caller can keep the column's processing exactly as it was.

    A block is a run of at least `_SUBCOLUMN_MIN_ROWS` consecutive lines
    in which one interior strip is empty in EVERY line, wide enough to be
    a gutter rather than a word space, with text on both sides in most of
    those lines and balanced sub-column widths -- a bullet column or a
    label/value table never qualifies.

    A horizontal rule across the gutter (``hrules``: (y, x0, x1)) ends one
    two-column block and starts the next: the Highlights flow down both
    columns and finish above a rule, and only then does the Contents list
    begin, in two columns of its own.
    """
    if len(rows) < _SUBCOLUMN_MIN_ROWS:
        return None
    x0 = min(w["x0"] for row in rows for w in row)
    x1 = max(w["x1"] for row in rows for w in row)
    lo_limit, hi_limit = x0 + _SUBCOLUMN_MIN_SIDE, x1 - _SUBCOLUMN_MIN_SIDE
    if hi_limit <= lo_limit:
        return None
    word_gap = _typical_word_gap(rows) or 1.5
    min_gap = max(_SUBCOLUMN_MIN_GAP, _SUBCOLUMN_GAP_PER_WORD_GAP * word_gap)

    def free_intervals(row: list[dict]) -> list[tuple[float, float]]:
        """Interior x-ranges this line leaves empty."""
        free = []
        cursor = lo_limit
        for a, b in _merge_intervals([(w["x0"], w["x1"]) for w in row], pad=0.0):
            if b <= cursor:
                continue
            if a > cursor:
                free.append((cursor, min(a, hi_limit)))
            cursor = max(cursor, b)
            if cursor >= hi_limit:
                break
        if cursor < hi_limit:
            free.append((cursor, hi_limit))
        return [(a, b) for a, b in free if b - a >= min_gap]

    frees = [free_intervals(row) for row in rows]
    n = len(rows)
    outward_break = max(12.0, 3.0 * min_gap)

    def adjacent_text(row: list[dict], gutter: tuple[float, float]) -> tuple[Optional[float], Optional[float]]:
        """Where this line's text touching the gutter starts (left side)
        and ends (right side), following words outward until a clear
        margin -- so a barcode strip further out is not counted as part
        of the sub-column, while a justified line's stretched word spaces
        (over 4pt on dense 6pt type) do not cut it short."""
        left = sorted((w for w in row if w["x1"] <= gutter[0] + 0.01), key=lambda w: -w["x1"])
        right = sorted((w for w in row if w["x0"] >= gutter[1] - 0.01), key=lambda w: w["x0"])
        start = end = None
        if left and gutter[0] - left[0]["x1"] < min_gap + 0.01:
            start = left[0]["x0"]
            for w in left[1:]:
                if start - w["x1"] >= outward_break:
                    break
                start = min(start, w["x0"])
        if right and right[0]["x0"] - gutter[1] < min_gap + 0.01:
            end = right[0]["x1"]
            for w in right[1:]:
                if w["x0"] - end >= outward_break:
                    break
                end = max(end, w["x1"])
        return start, end

    def qualifies(first: int, stop: int, gutter: tuple[float, float]) -> bool:
        segment = rows[first:stop]
        # Text on both sides of the strip in most lines...
        witnesses = sum(
            1 for row in segment
            if any(w["x1"] <= gutter[0] + 0.01 for w in row)
            and any(w["x0"] >= gutter[1] - 0.01 for w in row)
        )
        if witnesses < _SUBCOLUMN_MIN_WITNESS * len(segment):
            return False
        # ...and two sub-columns of comparable width, each measured on the
        # lines whose text reaches the gutter.
        edges = [adjacent_text(row, gutter) for row in segment]
        starts = sorted(a for a, _ in edges if a is not None)
        ends = sorted(b for _, b in edges if b is not None)
        if len(starts) < 5 or len(ends) < 5:
            return False
        left_w = gutter[0] - starts[len(starts) // 2]
        right_w = ends[len(ends) // 2] - gutter[1]
        if min(left_w, right_w) < _SUBCOLUMN_MIN_SIDE:
            return False
        return min(left_w, right_w) >= _BAND_WIDTH_BALANCE_RATIO * max(left_w, right_w)

    # Each empty strip is followed down the column on its own: a page
    # column can carry several at once (a barcode strip's margin beside a
    # Highlights gutter), and each is judged on its own run of lines.
    candidates: list[tuple[int, int, tuple[float, float]]] = []
    active: list[tuple[int, tuple[float, float]]] = []  # (first row, narrowed strip)
    for k in range(n + 1):
        row_free = frees[k] if k < n else []
        survivors: list[tuple[int, tuple[float, float]]] = []
        for first, (g0, g1) in active:
            overlaps = [
                (max(g0, a), min(g1, b)) for a, b in row_free if min(g1, b) - max(g0, a) >= min_gap
            ]
            if overlaps:
                survivors.append((first, max(overlaps, key=lambda g: g[1] - g[0])))
            elif k - first >= _SUBCOLUMN_MIN_ROWS:
                candidates.append((first, k, (g0, g1)))
        for a, b in row_free:
            if not any(g0 < b and a < g1 for _, (g0, g1) in survivors):
                survivors.append((k, (a, b)))
        active = survivors

    blocks: list[tuple[int, int, tuple[float, float]]] = []
    for first, stop, gutter in sorted(candidates, key=lambda c: -(c[1] - c[0])):
        if any(first < b_stop and b_first < stop for b_first, b_stop, _ in blocks):
            continue  # an overlapping, longer block already holds these lines
        if qualifies(first, stop, gutter):
            blocks.append((first, stop, gutter))
    blocks.sort()
    if not blocks:
        return None

    segments: list[tuple[list[dict], Optional[tuple[float, float]]]] = []
    cursor = 0
    for start, stop, gutter in blocks:
        if start > cursor:
            segments.append(([w for row in rows[cursor:start] for w in row], None))
        block_rows = rows[start:stop]
        top, bottom = _line_top(block_rows[0]), _line_top(block_rows[-1])
        cuts = sorted(
            y for y, rx0, rx1 in (hrules or [])
            if top < y < bottom and rx0 <= gutter[0] + 1.0 and rx1 >= gutter[1] - 1.0
        )
        piece: list[list[dict]] = []
        for row in block_rows:
            if piece and any(_line_top(piece[-1]) < y <= _line_top(row) for y in cuts):
                segments.append(([w for r in piece for w in r], gutter))
                piece = []
            piece.append(row)
        if piece:
            segments.append(([w for r in piece for w in r], gutter))
        cursor = stop
    if cursor < n:
        segments.append(([w for row in rows[cursor:] for w in row], None))
    return segments


def _should_reverse_rtl_columns(band_words: list[list[dict]]) -> bool:
    """Determine whether a multi-column RTL zone should be read right-to-left.

    Returns True for right-to-left column progression (e.g. traditional RTL columns).
    Returns False for left-to-right column progression (e.g. multi-panel proof flats,
    folding leaflets, or digital exports where Panel 1 is on the left).
    """
    if len(band_words) <= 1:
        return False

    first_band = band_words[0]
    last_band = band_words[-1]
    if not first_band or not last_band:
        return True

    first_text = " ".join(w.get("text", "") for w in first_band[:20])
    last_text = " ".join(w.get("text", "") for w in last_band[:20])
    last_tail = " ".join(w.get("text", "") for w in last_band[-20:])
    first_tail = " ".join(w.get("text", "") for w in first_band[-20:])

    _START_INDICATORS = {
        "تكوين", "وصف", "التركيب", "الوصف", "شكل", "المؤشرات", "دواعي", "1", "1.", "1)", "(1)",
        "Description", "Composition", "Indications",
    }
    _END_INDICATORS = {
        "صنع", "المصنع", "مصنع", "مراجعة", "تاريخ", "النسخة", "التخزين", "الصيدلانية",
        "Distributed", "Manufactured", "Storage",
    }

    has_start_in_first = any(ind in first_text for ind in _START_INDICATORS)
    has_end_in_last = any(ind in last_tail or ind in last_text for ind in _END_INDICATORS)

    has_start_in_last = any(ind in last_text for ind in _START_INDICATORS)
    has_end_in_first = any(ind in first_tail or ind in first_text for ind in _END_INDICATORS)

    if (has_start_in_first or has_end_in_last) and not (has_start_in_last or has_end_in_first):
        return False  # Left-to-right columns
    if (has_start_in_last or has_end_in_first) and not (has_start_in_first or has_end_in_last):
        return True  # Right-to-left columns

    return True  # Default traditional RTL column flow


def _line_top(line: list[dict]) -> float:
    """Average `top` of one clustered line -- a stable vertical sort key."""
    return sum(w["top"] for w in line) / len(line)


# --- Table rows with cells of different heights -----------------------
# A borderless table (rules above and below, none between the columns)
# is read line by line, and a row whose cells have different numbers of
# lines does not read the same way twice: a header cell "20 / mg/day"
# beside "Placebo" set vertically in the middle reads "20 40 80 120 160
# Placebo mg/day mg/day ..." in one layout and "20 Placebo 40 mg/day ..."
# in another, depending only on where each layout puts the middle line.
# Every table row with a multi-line cell became a block of false
# "inserted"/"deleted"/"moved" rows, even with identical tables.
#
# Such a row is recognised by a word that STRADDLES two lines -- it
# overlaps each of them by at least `_STRADDLE_OVERLAP` of its height,
# which consecutive lines of ordinary text never do -- and is then read
# cell by cell: the row's text split into cells at column-sized gaps,
# cells stacked into columns where they overlap horizontally, each
# column read top to bottom, columns left to right ("Placebo 20 mg/day
# 40 mg/day ..."). Lines with no straddling word are emitted exactly as
# before.
_STRADDLE_OVERLAP = 0.25
# The straddling word and the line it overlaps are similar in size: a
# large heading or drop cap beside two lines of body text is not a row.
_STRADDLE_SIZE_RATIO = 0.7
# A gap wider than this many times the text height separates two cells.
_CELL_GAP_EM = 0.7
# A table row's text is one size (smallest at least this share of the
# largest) and has at least this many columns.
_ROW_SIZE_UNIFORMITY = 0.8
_ROW_MIN_COLUMNS = 3


def _height(w: dict) -> float:
    return max(w.get("bottom", w["top"] + 10.0) - w["top"], 0.1)


def _straddles(a: dict, b: dict) -> bool:
    """Whether words ``a`` and ``b`` (on different lines) overlap
    vertically as cells of one table row do."""
    ha, hb = _height(a), _height(b)
    if min(ha, hb) < _STRADDLE_SIZE_RATIO * max(ha, hb):
        return False
    overlap = min(a.get("bottom", a["top"] + 10.0), b.get("bottom", b["top"] + 10.0)) - max(a["top"], b["top"])
    return overlap >= _STRADDLE_OVERLAP * min(ha, hb)


def _lines_straddle(upper: list[dict], lower: list[dict]) -> bool:
    """Whether any word of ``upper`` straddles any word of ``lower``.
    Only pairs that overlap vertically at all can (`_straddles` needs a
    positive overlap), so the rest are skipped unexamined -- lines of
    ordinary text usually have none."""
    lower_top = min(w["top"] for w in lower)
    reaching = [a for a in upper if a.get("bottom", a["top"] + 10.0) > lower_top]
    if not reaching:
        return False
    upper_bottom = max(a.get("bottom", a["top"] + 10.0) for a in reaching)
    return any(_straddles(a, b) for b in lower if b["top"] < upper_bottom for a in reaching)


def _read_lines(lines: list[list[dict]], *, rtl: bool) -> list[tuple[float, list[dict]]]:
    """(vertical key, words in reading order) for each line -- or, for
    lines a straddling word ties into one table row, for that row, read
    cell by cell (see the note above)."""
    ordered = sorted(lines, key=_line_top)
    # Lines a straddling (vertically centred) cell ties together first...
    groups: list[list[list[dict]]] = []
    for line in ordered:
        if groups and _lines_straddle(groups[-1][-1], line):
            groups[-1].append(line)
        else:
            groups.append([line])
    # ...then a plain line continuing the cells of the plain line above.
    # Only between single lines: a row held together by a centred cell
    # ("20 mg" beside a two-line description) has its first line empty
    # in the first column too, and is not a continuation of the header.
    # Where row labels stand: the first cell of every clearly tabular
    # line (three or more cells). A header stacked over the data columns
    # has nothing there.
    # A first cell standing over a value column ("Sildenafil" heading the
    # 20 mg column of a stacked header) is not a label.
    # Each line's cells, split once (the lines stay alive throughout, so
    # their ids are unique; any other word list is split as it comes).
    line_cells = {id(line): _line_cells(line) for line in ordered}

    def cells_of(words: list[dict]) -> list[list[dict]]:
        found = line_cells.get(id(words))
        return found if found is not None else _line_cells(words)

    tabular = [cells for cells in (cells_of(line) for line in ordered) if len(cells) >= _ROW_MIN_COLUMNS]

    def span(cell: list[dict]) -> tuple[float, float]:
        return min(w["x0"] for w in cell), max(w["x1"] for w in cell)

    value_spans = [span(cell) for cells in tabular for cell in cells[1:]]
    label_spans = [
        first
        for first in (span(cells[0]) for cells in tabular)
        if not any(first[0] < hi and lo < first[1] for lo, hi in value_spans)
    ]
    # The usual space between two table rows (median, consecutive
    # tabular lines): a label's second line sits closer than that.
    row_gaps = sorted(
        _gap(a, b)
        for a, b in zip(ordered, ordered[1:])
        if len(cells_of(a)) >= _ROW_MIN_COLUMNS and len(cells_of(b)) >= _ROW_MIN_COLUMNS
    )
    row_gap = row_gaps[len(row_gaps) // 2] if row_gaps else None
    header_ids = _table_header_lines(ordered, cells_of)
    rows: list[list[list[dict]]] = []
    plain: list[bool] = []  # row made of single lines only
    stacked: list[bool] = []  # row joined by continuation (2 columns are enough)
    headed: list[bool] = []  # row joined by a table header's lines
    carried: Optional[list[dict]] = None  # a label's first line, for the row below
    for k, group in enumerate(groups):
        if carried is not None:
            rows.append([carried] + list(group))
            plain.append(True)
            stacked.append(True)
            headed.append(False)
            carried = None
            continue
        # Against the whole row so far: its columns are those of all its
        # lines (a fourth header line "(n = 69) ... (n = 70)" reaches the
        # Placebo column the third, "20 mg 40 mg 80 mg", leaves empty).
        # (A header's line also continues a row a centred heading holds
        # together: "(%)" under "Adverse Event Term" beside "Placebo" over
        # "(N=112)".)
        header_line = id(group[0]) in header_ids
        up = bool(rows) and (plain[-1] or header_line) and len(group) == 1 and _continues_cells(
            rows[-1][0] if len(rows[-1]) == 1 else [w for line in rows[-1] for w in line],
            group[0], label_spans, row_gap, cells_of=cells_of,
            header=header_line,
        )
        # A label alone on its line belongs to the row it sits closer to:
        # "Limb" under "Pain in | 7% | ..." ends that label; "Extrapyramidal"
        # over "Disorder** | 6 | ..." starts the next one.
        below = groups[k + 1][0] if k + 1 < len(groups) and len(groups[k + 1]) == 1 else None
        down = len(group) == 1 and below is not None and _starts_label(group[0], below, cells_of=cells_of)
        if down and (not up or _gap(group[0], below) < _gap(rows[-1][-1], group[0])):
            carried = group[0]
            continue
        if not up and rows and len(group) > 1 and all(id(line) in header_ids for line in group):
            # A header's staggered lines a straddle tied together: each in
            # turn continues the row.
            so_far = [w for line in rows[-1] for w in line]
            up = True
            for line in group:
                if not _continues_cells(so_far, line, label_spans, row_gap, cells_of=cells_of, header=True):
                    up = False
                    break
                so_far = so_far + line
            if up:
                rows[-1].extend(group)
                stacked[-1] = True
                headed[-1] = True
                continue
        if up:
            rows[-1].append(group[0])
            stacked[-1] = True
            headed[-1] = headed[-1] or header_line
        else:
            rows.append(list(group))
            plain.append(len(group) == 1)
            stacked.append(False)
            headed.append(False)
    if carried is not None:
        rows.append([carried])
        plain.append(True)
        stacked.append(False)
        headed.append(False)
    out: list[tuple[float, list[dict]]] = []
    for row, by_continuation, by_header in zip(rows, stacked, headed):
        # (A header fills every column on every line -- "Placebo | 20
        # mg/day ..." over "(n=696) | (n=71) ..." -- and is still one row.)
        by_cells = (
            _read_row_by_cells(row, rtl=rtl, min_columns=2 if by_continuation else _ROW_MIN_COLUMNS)
            if len(row) > 1 and (by_header or not _complete_rows(row, cells_of))
            else None
        )
        if by_cells is None:
            out.extend((_line_top(line), _sort_line_words(line, rtl=rtl)) for line in row)
        else:
            out.append((_line_top(row[0]), by_cells))
    return out


def _complete_rows(row: list[list[dict]], cells_of) -> bool:
    """Whether the lines a centred cell ties together are each a whole
    table row of their own -- every line under the first holding the same
    3+ cells, the first those plus the centred one -- to be read line by
    line, not cell by cell. A study number centred beside its three arms
    ("1" | "Lurasidone (40 mg/day)* | 54.2 (8.8) | -9.4 (1.6) | ...") was
    read column by column: three labels, then three baselines, then three
    changes -- against a ruled table read row by row. A cell wrapped over
    two lines leaves fewer cells on its second line, or none under the row
    label ("All Patients | -1.9 | -1.1" over "(n=672) | (n=70)"), and is
    read by cells."""
    cells = [sorted(cells_of(line), key=lambda c: min(w["x0"] for w in c)) for line in row]
    counts = [len(c) for c in cells]
    rest = counts[1:]
    if not (min(rest) >= _ROW_MIN_COLUMNS and len(set(rest)) == 1 and counts[0] in (rest[0], rest[0] + 1)):
        return False
    extra = counts[0] - rest[0]
    if extra and not all(_NUMBERING_LABEL.match(w["text"]) for w in cells[0][0]):
        return False  # one cell more, and not a centred number: a cell of the row wrapped
    label = cells[0][extra]  # the first line's row label, after a centred number
    lo, hi = min(w["x0"] for w in label), max(w["x1"] for w in label)
    return all(
        min(w["x0"] for w in line_cells[0]) < hi and lo < max(w["x1"] for w in line_cells[0])
        for line_cells in cells[1:]
    )


def _line_cells(line: list[dict]) -> list[list[dict]]:
    """One line split into table cells at column-sized gaps."""
    words = sorted(line, key=lambda w: w["x0"])
    cells = [[words[0]]]
    for w in words[1:]:
        if w["x0"] - cells[-1][-1]["x1"] > _CELL_GAP_EM * min(_height(w), _height(cells[-1][-1])):
            cells.append([w])
        else:
            cells[-1].append(w)
    return cells


def _continues_cells(
    above: list[dict],
    line: list[dict],
    label_spans: Optional[list[tuple[float, float]]] = None,
    row_gap: Optional[float] = None,
    cells_of=_line_cells,
    header: bool = False,
) -> bool:
    """Whether ``line`` is the second line of the cells of ``above``.

    ``header``: ``line`` is one of the lines of a table's header (see
    `_table_header_lines`), which stack whatever sits in the label column.

    Either a table row whose cells all wrap ("-1.9" over "(n=672)" in
    every column) with its label on the first line only -- nothing of
    ``line`` sits under the first column -- or a header stacked in
    columns ("Lurasidone | Lurasidone" over "hydrochloride tablets |
    hydrochloride tablets"), where neither line starts where the table's
    rows have their labels (``label_spans``: the first cell of each
    tabular line). In
    both, each cell of ``line`` sits under a different cell of
    ``above``: a spanning header ("Lurasidone hydrochloride" over five
    dose columns) has several under one, and an ordinary data row has
    its own label in the first column."""
    top_cells = cells_of(above)
    cells = cells_of(line)
    if len(cells) == 1 and header:
        # One column's heading running on alone (see `_table_header_lines`).
        return _under_value_column(cells[0], top_cells) and _gap(above, line) <= 0.5 * min(
            _height(w) for w in above
        )
    if len(cells) == 1 and len(top_cells) >= _ROW_MIN_COLUMNS:
        # Only clearly closer than rows are to each other: the second
        # line of a label, not the first line of the next row's.
        if row_gap is None or _gap(above, line) > _LABEL_LINE_GAP_SHARE * row_gap:
            return False
        return _continues_label(top_cells, cells[0])
    if len(top_cells) < 2 or len(cells) < 2:
        return False
    if min(w["x0"] for w in line) <= max(w["x1"] for w in top_cells[0]):
        # Something under the first cell above: only a stacked header,
        # both lines clear of the label column, continues it.
        # Indented bulleted items set in columns ("o tingling   o
        # numbness") line up the same way and are not a header.
        def in_label_column(part: list[dict]) -> bool:
            first = cells_of(part)[0]
            x0, x1 = min(w["x0"] for w in first), max(w["x1"] for w in first)
            return any(x0 < hi and lo < x1 for lo, hi in label_spans or [])

        stub = (
            bool(label_spans)
            and not in_label_column(above)
            and not in_label_column(line)
            and not any(
                min(part, key=lambda w: w["x0"])["text"] in _LIST_BULLETS
                for part in (above, line)
            )
        )
        if not stub and not header and not _header_continuation(cells):
            return False
    elif len(top_cells) < _ROW_MIN_COLUMNS:
        return False
    used: set[int] = set()
    top_x0 = min(w["x0"] for w in above)
    for n_cell, cell in enumerate(cells):
        x0, x1 = min(w["x0"] for w in cell), max(w["x1"] for w in cell)
        under = [
            k for k, top in enumerate(top_cells)
            if x0 < max(w["x1"] for w in top) and min(w["x0"] for w in top) < x1
        ]
        if not under and header and (n_cell > 0 or x1 < top_x0):
            # A heading set lower than the rest, under an empty cell: the
            # label column's ("Adverse Event Term"), or a column's whose
            # neighbours run to more lines ("Placebo" under nothing, beside
            # "Lurasidone / hydrochloride / 40 mg/day").
            continue
        if len(under) != 1 or under[0] in used:
            return False
        used.add(under[0])
    if not used:
        return False
    # Directly below: no more than a normal line's leading apart.
    gap = min(w["top"] for w in line) - max(w.get("bottom", w["top"] + 10.0) for w in above)
    return gap <= 0.5 * min(_height(w) for w in above)


# A column heading's own lines below its name: a group size, a unit, a
# dose unit ("(N=708)", "(%)", "mg/day", "(n = 69)").
_HEADER_CELL = re.compile(r"^\((?:n\s*=\s*[\d,]+|%)\)$|^(?:mg|mcg|µg|g|mL|%)(?:/\w+)?$", re.IGNORECASE)


# A data cell of a table: a count, a percentage, a mean with its spread,
# a fraction of patients, or a dash for none ("9", "<1%", "54.2 (8.8)",
# "-9.4", "(15/263)", "—").
_DATA_CELL = re.compile(
    r"^(?:[<>≤≥~]\s*)?[-+−]?\d[\d.,]*\s*%?(?:\s*\(.*\))?$|^\(\d[\d,]*/\d[\d,]*\)$|^(?:—|–|-|NA|N/A)$",
    re.IGNORECASE,
)
# A table header is at most this many lines, each of two or more cells,
# with at most this many one-cell lines (a group heading) before the first
# data row.
_HEADER_MAX_LINES = 6
_HEADER_MAX_LABEL_CELLS = 3
# What makes a number a measurement rather than a dose heading.
_MEASURE_MARKS = re.compile(r"[.%+\-−<>≤≥]")
_HEADER_MAX_GROUP_LINES = 2


def _table_header_lines(ordered: list[list[dict]], cells_of=_line_cells) -> set[int]:
    """ids of the lines that are a table's header: the lines of two or
    more cells directly above its first data row (a label, then nothing
    but numbers). The same header was read cell by cell where Word drew it
    ruled ("Lurasidone hydrochloride 20 mg/day (N=71)") and line by line
    where an artwork set it unruled ("Lurasidone Lurasidone ... Parameter
    (N=708) hydrochloride hydrochloride ..."), and every header showed as
    deleted and inserted. Lines of a header stack in columns even over the
    label column, which data rows never do (see `_continues_cells`)."""
    if not any(len(cells_of(line)) >= _ROW_MIN_COLUMNS for line in ordered):
        return set()
    found: set[int] = set()
    run: list[list[dict]] = []
    group_lines = 0
    previous: Optional[list[dict]] = None
    for line in ordered:
        cells = cells_of(line)
        centred = previous is not None and _gap(previous, line) < 0
        previous = line
        if len(cells) < 2 and run and _under_value_column(cells[0], cells_of(run[0])):
            # One column's heading running to more lines than its
            # neighbours', staggered ("(N=334)" under "Placebo", then "20
            # to 120 mg/day" under "Lurasidone hydrochloride").
            run.append(line)
            group_lines = 0
            continue
        if len(cells) < 2 and centred and run:
            continue  # the label column's heading, centred beside the header's lines
        if len(cells) < 2:
            group_lines += 1
            if group_lines > _HEADER_MAX_GROUP_LINES:
                run = []
            continue
        texts = [" ".join(w["text"] for w in cell) for cell in cells]
        # A data row: its label -- one to three cells, a study number and
        # an arm among them ("1 | Lurasidone hydrochloride | 54.2 (8.8) |
        # ...") -- then numbers only; two of them, or one measurement
        # ("Total cholesterol | 5.4%", its other column's "5.7%" staggered).
        trailing = 0
        for text in reversed(texts):
            if not _DATA_CELL.match(text):
                break
            trailing += 1
        leading = texts[:len(texts) - trailing]
        is_data = (
            1 <= len(leading) <= _HEADER_MAX_LABEL_CELLS
            and any(ch.isalpha() for text in leading for ch in text)
            and (trailing >= 2 or (trailing == 1 and bool(_MEASURE_MARKS.search(texts[-1]))))
        )
        if is_data:
            # Lines wholly left of the row's numbers are the first lines of
            # its label ("Monotherapy study | Lurasidone hydrochloride" over
            # "(20 to 60 mg/day)* | 30.3 (5.0) | ..."), not the header's.
            first_value = min(w["x0"] for w in cells[len(cells) - trailing])
            while run and max(w["x1"] for w in run[-1]) < first_value:
                run.pop()
            if 2 <= len(run) <= _HEADER_MAX_LINES:
                found.update(id(header_line) for header_line in run)
            run = []
        elif all(_DATA_CELL.match(t) and _MEASURE_MARKS.search(t) for t in texts):
            # Values whose label stands on lines of its own ("Total" /
            # "Cholesterol" beside "5.3% | 13.8% ..."): a data row, never
            # a header -- which holds names, sizes and doses ("20 | 40").
            run = []
        else:
            if group_lines:
                run = []  # a one-cell line inside a header ends it
            run.append(line)
        group_lines = 0
    return found


def _under_value_column(cell: list[dict], top_cells: list[list[dict]]) -> bool:
    """Whether ``cell`` lies under exactly one of ``top_cells`` other than
    the first (the label column's), centred within it -- a heading set
    across columns ("Mean Change from Baseline (mg/dL)", over three dose
    columns) reaches into one of them, off its centre."""
    x0, x1 = min(w["x0"] for w in cell), max(w["x1"] for w in cell)
    under = [
        k for k, top in enumerate(top_cells)
        if x0 < max(w["x1"] for w in top) and min(w["x0"] for w in top) < x1
    ]
    if len(top_cells) < 2 or len(under) != 1 or under[0] == 0:
        return False
    top = top_cells[under[0]]
    return min(w["x0"] for w in top) <= (x0 + x1) / 2.0 <= max(w["x1"] for w in top)


def _header_continuation(cells: list[list[dict]]) -> bool:
    """Whether a line's cells after its first are mostly a header's
    lower lines ("Organ Class | (N=708) | mg/day | mg/day ..." under "Body
    System or | Placebo | 20 | 40 ..."). Such a line continues the header
    above it even under the row-label column -- where a data row ("Vomiting
    | 3 | 4 ...") never does: its cells are numbers. Read line by line, the
    header's names and sizes came out apart, against a ruled table's cells
    read whole ("Placebo (N=708) (%)")."""
    values = [" ".join(w["text"] for w in cell) for cell in cells[1:]]
    if len(values) < 2:
        return False
    return sum(1 for v in values if _HEADER_CELL.match(v)) * 2 > len(values)


def _continues_label(top_cells: list[list[dict]], cell: list[dict]) -> bool:
    """Whether ``cell``, alone on its line, is the rest of the row label
    of the table row above ("Pain in" with its values, then "Limb"):
    it starts exactly where that label starts, within the label's own
    column, directly below. A group heading below a row ("Nervous
    System Disorders") is set at its own indent and stays a line."""
    label = top_cells[0]
    x0 = min(w["x0"] for w in cell)
    if abs(x0 - min(w["x0"] for w in label)) > _LABEL_INDENT_TOLERANCE:
        return False
    if max(w["x1"] for w in cell) >= min(w["x0"] for w in top_cells[1]):
        return False  # runs into the value columns: not inside the label column
    gap = min(w["top"] for w in cell) - max(w.get("bottom", w["top"] + 10.0) for c in top_cells for w in c)
    return gap <= 0.5 * min(_height(w) for w in label)


_LABEL_INDENT_TOLERANCE = 1.0
_LABEL_LINE_GAP_SHARE = 0.6


def _gap(upper: list[dict], lower: list[dict]) -> float:
    """Vertical space between two lines."""
    return min(w["top"] for w in lower) - max(w.get("bottom", w["top"] + 10.0) for w in upper)


def _starts_label(line: list[dict], below: list[dict], cells_of=_line_cells) -> bool:
    """Whether ``line``, a single cell alone on its line, is the first
    line of the row label of the table row directly below it
    ("Extrapyramidal" over "Disorder** | 6 | 6 | 11 ...")."""
    cells = cells_of(line)
    below_cells = cells_of(below)
    if len(cells) != 1 or len(below_cells) < _ROW_MIN_COLUMNS:
        return False
    label = below_cells[0]
    if abs(min(w["x0"] for w in line) - min(w["x0"] for w in label)) > _LABEL_INDENT_TOLERANCE:
        return False
    if max(w["x1"] for w in line) >= min(w["x0"] for w in below_cells[1]):
        return False
    return _gap(line, below) <= 0.5 * min(_height(w) for w in label)


def _read_row_by_cells(
    row: list[list[dict]], *, rtl: bool, min_columns: int = _ROW_MIN_COLUMNS
) -> Optional[list[dict]]:
    """One table row spanning several lines, read cell by cell -- or
    None when the lines are not a table row: text of mixed sizes (a
    title set beside a second-language title), or fewer than
    ``min_columns`` columns."""
    heights = [_height(w) for line in row for w in line]
    if min(heights) < _ROW_SIZE_UNIFORMITY * max(heights):
        return None
    # Each line split into cells at column-sized gaps.
    cells = [cell for line in row for cell in _line_cells(line)]
    # Cells overlapping horizontally belong to one column of the row.
    spans = [(min(w["x0"] for w in c), max(w["x1"] for w in c)) for c in cells]
    parent = list(range(len(cells)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(cells)):
        for j in range(i + 1, len(cells)):
            if spans[i][0] < spans[j][1] and spans[j][0] < spans[i][1]:
                parent[find(i)] = find(j)
    columns: dict[int, list[list[dict]]] = {}
    for i, cell in enumerate(cells):
        columns.setdefault(find(i), []).append(cell)
    if len(columns) < min_columns:
        return None
    # Three or more lines that each fill (nearly) every column are rows
    # of their own sharing one merged cell -- a study number centred
    # beside its three treatment groups ("1 | Lurasidone 40 mg | 54.2
    # (8.8) | -9.4 (1.6)" ...) -- not one row of wrapped cells. Read cell
    # by cell, every value of a column would run together.
    # (Only for a row a centred cell holds together: a header continued
    # line by line has every line full, and is one row.)
    complete = sum(1 for line in row if len(_line_cells(line)) >= len(columns) - 1)
    if min_columns >= _ROW_MIN_COLUMNS and complete >= 3:
        return None
    ordered_columns = sorted(
        columns.values(), key=lambda col: min(w["x0"] for c in col for w in c), reverse=rtl
    )
    words: list[dict] = []
    for column in ordered_columns:
        for cell in sorted(column, key=_line_top):
            words.extend(_sort_line_words(cell, rtl=rtl))
    return words


def _word_is_rtl_for_sorting(text: str, default: bool = True) -> bool:
    """Classify a word for line-level bidi run sorting.

    Words containing RTL letters/marks are RTL.
    Words containing strong Latin letters or digits are LTR.
    Purely neutral words (punctuation, brackets, symbols) resolve to `default`
    (e.g. True in an RTL line, so closing brackets stay with their RTL phrase).
    """
    has_rtl = any(
        unicodedata.bidirectional(c) in _RTL_BIDI_TYPES
        for c in text
    )
    if has_rtl:
        return True
    has_ltr_or_digit = any(
        unicodedata.bidirectional(c) in _LTR_BIDI_TYPES or ('0' <= c <= '9')
        for c in text
    )
    if has_ltr_or_digit:
        return False
    return default


def _sort_line_words(line: list[dict], *, rtl: bool) -> list[dict]:
    """Order one visual line's word-dicts for reading.

    For an LTR line this is just ascending x0. For an RTL line, a
    naive "sort everything by descending x0" reverses the RELATIVE
    ORDER of any embedded LTR/numeric run too -- a real observed case:
    a batch code was extracted as ONE word ("0-31") in one PDF but as
    TWO word-fragments ("0" and "-31") in the other, purely from a
    kerning/font difference between the two files. Sorting the whole
    line by descending x0 swapped those two fragments' relative order
    ("-31" then "0"), corrupting the value into "-31 0" and producing a
    false discrepancy against the other document's correctly-ordered
    "0-31" -- even though the actual content was identical.

    This instead groups the line into RUNS of consecutive same-
    direction words (a word with no RTL letters at all -- digits,
    Latin, punctuation-only -- joins an LTR/"weak" run, exactly like
    `_fix_rtl_word_chars` treats an embedded digit/Latin run WITHIN one
    word), reverses the ORDER of runs (the line reads right-to-left
    overall) while an RTL run's own words reverse and an LTR run's
    words keep their natural ascending order -- so fragments of one
    embedded LTR value stay in their own correct relative order no
    matter how many pieces pdfplumber happened to split them into.
    """
    ordered = sorted(line, key=lambda rw: rw["x0"])
    if not rtl:
        return ordered

    runs: list[tuple[bool, list[dict]]] = []
    for w in ordered:
        is_rtl_word = _word_is_rtl_for_sorting(w["text"], default=True)
        if runs and runs[-1][0] == is_rtl_word:
            runs[-1][1].append(w)
        else:
            runs.append((is_rtl_word, [w]))
    # `runs` was built by walking `ordered` (ascending x0), so it is
    # already in ascending run-position order -- reversing the list
    # itself gives the right-to-left run sequence.
    runs.reverse()

    result: list[dict] = []
    for is_rtl_word, words in runs:
        result.extend(sorted(words, key=lambda rw: rw["x0"], reverse=is_rtl_word))
    return result


def _find_table_column_gutters(
    c_words_non_header: list[dict], min_gutter: float = 3.0
) -> list[tuple[float, float]]:
    c_rows = sorted(_group_into_lines(c_words_non_header), key=_line_top)
    if len(c_rows) < 2:
        return []
    candidate_splits = []
    for row in c_rows:
        r_sorted = sorted(row, key=lambda w: w["x0"])
        for a, b in zip(r_sorted, r_sorted[1:]):
            if (b["x0"] - a["x1"]) >= min_gutter:
                candidate_splits.append((a["x1"], b["x0"]))
    if not candidate_splits:
        return []
    min_x = min(s[0] for s in candidate_splits)
    max_x = max(s[1] for s in candidate_splits)
    gutters = []
    step = 0.5
    x = min_x
    in_gutter = False
    g_start = 0.0
    while x <= max_x:
        crossing = any(w["x0"] - 0.2 <= x <= w["x1"] + 0.2 for row in c_rows for w in row)
        if not crossing:
            if not in_gutter:
                in_gutter = True
                g_start = x
        else:
            if in_gutter:
                in_gutter = False
                if (x - g_start) >= min_gutter:
                    gutters.append((g_start, x))
        x += step
    if in_gutter and (max_x - g_start) >= min_gutter:
        gutters.append((g_start, max_x))
    return gutters


def _gutters_to_bands(
    gutters: list[tuple[float, float]], c_x0: float, c_x1: float
) -> list[tuple[float, float]]:
    if not gutters:
        return [(c_x0, c_x1)]
    bands = []
    prev = c_x0
    for g_lo, g_hi in gutters:
        if g_lo > prev:
            bands.append((prev, g_lo))
        prev = g_hi
    if prev < c_x1:
        bands.append((prev, c_x1))
    return bands


def _table_units_for(
    t_idx: int,
    entries: list[tuple[int, dict]],
    bands: list[tuple[float, float]],
    cell_ids: dict[int, tuple[int, int, int]],
    table_bbox: tuple[float, float, float, float],
    *,
    band_idx: Optional[int] = None,
    rtl: bool = False,
    inner_ids: Optional[dict[int, tuple[int, int, int]]] = None,
    tables: Optional[list[dict]] = None,
) -> list[tuple[int, float, list[dict]]]:
    """One table's words -> (band_index, cell_top, words) per populated cell.

    Cells are emitted row-major: row index, then column index. Words
    within a cell are grouped into visual lines and ordered top-to-
    bottom, left-to-right (or right-to-left when ``rtl`` is True, using
    the same embedded-LTR-run-aware ordering as body text -- see
    `_sort_line_words`). Every unit carries the band index of the
    table's horizontal center plus the top of its cell, so the caller
    can interleave table rows into that band by vertical position.

    In multi-arm tables where multiple treatment columns lack vertical
    ruling lines and get lumped into single cells, continuous whitespace
    gutters across non-header rows are detected to order words
    sub-column by sub-column rather than horizontal line interleaving.
    """
    by_cell: dict[tuple[int, int], list[dict]] = {}
    by_cell_entries: dict[tuple[int, int], list[tuple[int, dict]]] = {}
    words_by_col: dict[int, list[dict]] = {}
    non_header_by_col: dict[int, list[dict]] = {}
    for w_idx, w in entries:
        r, c = cell_ids[w_idx][1:]
        by_cell.setdefault((r, c), []).append(w)
        by_cell_entries.setdefault((r, c), []).append((w_idx, w))
        words_by_col.setdefault(c, []).append(w)
        if r >= 1:
            non_header_by_col.setdefault(c, []).append(w)

    col_sub_bands: dict[int, list[tuple[float, float]]] = {}
    for c, c_words in words_by_col.items():
        if c == 0 or len(c_words) < 6:
            continue
        c_x0 = min(w["x0"] for w in c_words)
        c_x1 = max(w["x1"] for w in c_words)
        if (c_x1 - c_x0) < 40.0:
            continue
        nh_words = non_header_by_col.get(c, [])
        if len(nh_words) < 4:
            continue
        gutters = _find_table_column_gutters(nh_words, min_gutter=3.0)
        if gutters:
            detected = _gutters_to_bands(gutters, c_x0, c_x1)
            if len(detected) >= 2:
                col_sub_bands[c] = detected

    if band_idx is None:
        band_idx = _band_index_for((table_bbox[0] + table_bbox[2]) / 2.0, bands)

    row_tops: dict[int, float] = {}
    for (r, c), words in by_cell.items():
        if words:
            c_min = min(w["top"] for w in words)
            if r not in row_tops or c_min < row_tops[r]:
                row_tops[r] = c_min

    # A grid row with no label whose cells all sit under filled cells of
    # the labelled row above is the second line of those cells: an SPL
    # draws "+0.10" and "(n=103)" in separate grid rows of one record
    # ("All Patients | +0.10 | ..." then " | (n=103) | ..."), where a Word
    # draft has one row of two-line cells. Read cell by cell -- "+0.10
    # (n=103) +0.75 (n=102)" -- as the borderless table is.
    columns_of: dict[int, set[int]] = {}
    for (r, c), words in by_cell.items():
        if words:
            columns_of.setdefault(r, set()).add(c)
    record_of: dict[int, int] = {}
    # The header too: the grid rows of two or more cells, none in the label
    # column, directly above the first labelled row are one header, read
    # column by column -- "Placebo | Lurasidone hydrochloride tablets 40
    # mg/day | ..." -- as a stacked header set without rules is (see
    # `_table_header_lines`). Read row by row, an SPL's header ("Lurasidone
    # hydrochloride tablets | Lurasidone ..." over "Placebo | 40 mg/day |
    # 80 mg/day") came out apart, against its Word draft's whole cells.
    # (Under the table's caption, when the grid draws that as its first
    # row: "Table 10. Weight Changes (kg) ..." across the whole table.)
    first_row = min(columns_of, default=0)
    for labelled in sorted(r for r, cols in columns_of.items() if 0 in cols):
        header_rows: list[int] = []
        r = labelled - 1
        while r in columns_of and 0 not in columns_of[r] and len(columns_of[r]) >= 2:
            header_rows.insert(0, r)
            r -= 1
        at_top = r < first_row or (r == first_row and len(columns_of[r]) == 1)
        if header_rows and at_top and 2 <= len(header_rows) <= _HEADER_MAX_LINES:
            for r in header_rows:
                record_of[r] = header_rows[0]
        if labelled > first_row:
            break
    record = None  # the labelled row the rows just read belong to
    previous = None
    for r in sorted(columns_of):
        cols = columns_of[r]
        if r in record_of:  # a header row
            record = None
        elif 0 in cols:
            record = r
            record_of[r] = r
        elif record is not None and previous is not None and record_of[previous] == record \
                and r == previous + 1 and cols <= columns_of[record]:
            record_of[r] = record
        else:
            record = None
            record_of[r] = r
        previous = r

    units: list[tuple[int, float, list[dict]]] = []
    for (r, c) in sorted(by_cell):
        words = by_cell[(r, c)]
        sub_bands = col_sub_bands.get(c, [])
        nested: dict[int, list[tuple[int, dict]]] = {}
        for w_idx, w in by_cell_entries[(r, c)] if inner_ids else ():
            inner = inner_ids.get(w_idx)
            if inner is not None and inner[0] != t_idx:
                nested.setdefault(inner[0], []).append((w_idx, w))
        if nested and tables is not None:
            # The cell holds a whole table of its own: read that table
            # cell by cell, row by row, as the same table is read where
            # it stands alone -- never line by line across its columns,
            # which runs one cell's words into the next ("Placebo
            # MOUNJARO MOUNJARO Reaction (N=235) ..."). Text above or
            # below it in the cell (a caption, a note) keeps its place.
            blocks: list[tuple[float, list[dict]]] = []
            for j, inner_entries in nested.items():
                inner_units = _table_units_for(
                    j, inner_entries, [(tables[j]["bbox"][0], tables[j]["bbox"][2])], inner_ids,
                    tables[j]["bbox"], band_idx=0, rtl=rtl,
                )
                blocks.append(
                    (tables[j]["bbox"][1], [w for _b, _k, ws in sorted(inner_units, key=lambda u: u[1]) for w in ws])
                )
            in_nested = {id(w) for inner_entries in nested.values() for _i, w in inner_entries}
            loose = [w for w in words if id(w) not in in_nested]
            if loose:
                blocks.extend(_read_lines(_group_into_lines(loose), rtl=rtl))
            cell_words = [w for _top, block in sorted(blocks, key=lambda b: b[0]) for w in block]
        elif len(sub_bands) <= 1 or not words:
            cell_words: list[dict] = []
            # A cell the grid draws around a whole stacked header -- or
            # around a whole table, with no rules between its columns --
            # holds a small table of its own: read it as one.
            for _top, line_words in _read_lines(_group_into_lines(words), rtl=rtl):
                cell_words.extend(line_words)
        else:
            ordered_sub_bands = list(reversed(sub_bands)) if rtl else sub_bands
            cell_words = []
            for sb_x0, sb_x1 in ordered_sub_bands:
                sb_words = [w for w in words if sb_x0 <= (w["x0"] + w["x1"]) / 2.0 <= sb_x1]
                if not sb_words:
                    continue
                for line in _group_into_lines(sb_words):
                    cell_words.extend(_sort_line_words(line, rtl=rtl))
            assigned = {id(w) for w in cell_words}
            remaining = [w for w in words if id(w) not in assigned]
            if remaining:
                for line in _group_into_lines(remaining):
                    cell_words.extend(_sort_line_words(line, rtl=rtl))

        col_offset = (1000 - c) * 1e-4 if rtl else c * 1e-4
        owner = record_of.get(r, r)
        sort_key = row_tops.get(owner, min(w["top"] for w in cell_words)) + col_offset + (r - owner) * 1e-6
        units.append((band_idx, sort_key, cell_words))
    return units


def _band_index_for(mid_x: float, bands: list[tuple[float, float]]) -> int:
    """Index of the band whose range contains (or is nearest to) ``mid_x``."""
    best_idx = 0
    best_dist: Optional[float] = None
    for idx, (lo, hi) in enumerate(bands):
        if lo <= mid_x <= hi:
            return idx
        dist = min(abs(mid_x - lo), abs(mid_x - hi))
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_idx = idx
    return best_idx


def _typical_word_gap(rows: list[list[dict]]) -> Optional[float]:
    """Median within-line adjacent-word gap across ``rows``, or None if
    there isn't enough data (fewer than a handful of measurable gaps --
    e.g. a page of single-word lines). Used to scale column-gutter
    detection to THIS page's own font size instead of one constant
    tuned for a single document (see MIN_COLUMN_GUTTER_WIDTH's
    docstring)."""
    gaps: list[float] = []
    for row in rows:
        ordered = sorted(row, key=lambda w: w["x0"])
        for a, b in zip(ordered, ordered[1:]):
            gap = b["x0"] - a["x1"]
            if 0.0 <= gap <= _TYPICAL_GAP_OUTLIER_CUTOFF:
                gaps.append(gap)
    if len(gaps) < 5:
        return None
    gaps.sort()
    mid = len(gaps) // 2
    if len(gaps) % 2:
        return gaps[mid]
    return (gaps[mid - 1] + gaps[mid]) / 2.0


def _adaptive_min_gutter(rows: list[list[dict]]) -> float:
    """Minimum column-gutter width for THIS page: at least
    GUTTER_WIDTH_MULTIPLIER times the page's own typical word gap,
    floored at MIN_GUTTER_WIDTH_FLOOR and capped at 25.0pt, falling back to
    MIN_COLUMN_GUTTER_WIDTH when the page has too little data to
    measure its own spacing."""
    typical_gap = _typical_word_gap(rows)
    if typical_gap is None:
        return MIN_COLUMN_GUTTER_WIDTH
    # The floor scales down with type too small for it: a foil set in
    # 2.8pt type has its two columns 6pt apart -- two text heights, a wide
    # gutter at that size -- and was read across, line by line.
    heights = sorted(
        w["bottom"] - w["top"] for row in rows for w in row if w.get("bottom", 0.0) > w.get("top", 0.0)
    )
    floor = MIN_GUTTER_WIDTH_FLOOR
    if heights:
        floor = min(floor, _GUTTER_FLOOR_HEIGHTS * heights[len(heights) // 2])
    return min(25.0, max(floor, typical_gap * GUTTER_WIDTH_MULTIPLIER))


def _detect_column_bands(
    rows: list[list[dict]],
    content_x0: float,
    content_x1: float,
    min_gutter: float = MIN_COLUMN_GUTTER_WIDTH,
    tolerance_fraction: float = COLUMN_GUTTER_ROW_TOLERANCE_FRACTION,
    _zero_first: bool = True,
) -> list[tuple[float, float]]:
    """Find left-to-right column x-ranges for one page's rows.

    See the module docstring for the full algorithm. Returns a single
    band spanning the whole content width when no persistent interior
    gutter is found (i.e. an ordinary single-column page).
    """
    total_rows = len(rows)
    if total_rows == 0 or content_x1 <= content_x0:
        return [(content_x0, content_x1)]

    x0i = int(math.floor(content_x0))
    x1i = int(math.ceil(content_x1))
    width = max(1, x1i - x0i)

    # Difference array over 1pt-wide buckets: +1 at a row-span's start,
    # -1 just after its end. A prefix sum then gives, at every x, how
    # many distinct ROWS have some word covering that point.
    diff = [0] * (width + 2)
    # The exact (fractional) text edges bounding each bucket: the
    # furthest a run of text reaches into the last bucket it covers, and
    # where one starts in its first. A gutter's true width is measured
    # between these, not by counting whole buckets (see below).
    right_edge_at: dict[int, float] = {}
    left_edge_at: dict[int, float] = {}
    for row in rows:
        intervals = _merge_intervals([(w["x0"], w["x1"]) for w in row])
        for a, b in intervals:
            lo = max(0, int(math.floor(a)) - x0i)
            hi = min(width, int(math.ceil(b)) - x0i)
            if hi < lo:
                hi = lo
            diff[lo] += 1
            diff[hi + 1] -= 1
            if b > right_edge_at.get(hi, -math.inf):
                right_edge_at[hi] = b
            if a < left_edge_at.get(lo, math.inf):
                left_edge_at[lo] = a

    counts = [0] * (width + 1)
    running = 0
    for k in range(width + 1):
        running += diff[k]
        counts[k] = running

    if tolerance_fraction > 0 and _zero_first:
        zero_bands = _detect_column_bands(
            rows, content_x0, content_x1, min_gutter=min_gutter, tolerance_fraction=0.0
        )
        if len(zero_bands) >= 2:
            # Gutters no row crosses come first, with their exact edges
            # (the tolerant pass below widens a gutter into ragged line
            # endings). But a few full-width lines -- an artwork
            # information block or a note above the columns -- can cross
            # REAL gutters, and those columns then merge into one band
            # read row by row across all of them. On one outsert, two
            # lines at the top of the sheet merged three columns
            # (514-1521pt), interleaving body text with the tables beside
            # it, and the comparison reported 512 "moved" fragments. So
            # a gutter the tolerant pass finds strictly INSIDE a zero
            # band still splits it.
            tolerant = _detect_column_bands(
                rows, content_x0, content_x1, min_gutter=min_gutter,
                tolerance_fraction=tolerance_fraction, _zero_first=False,
            )
            return _split_bands_at_interior_gutters(zero_bands, tolerant)

    threshold = max(1, round(total_rows * tolerance_fraction)) if tolerance_fraction > 0 else 0

    gaps: list[tuple[float, float]] = []
    k = 0
    while k <= width:
        if counts[k] <= threshold:
            start = k
            while k <= width and counts[k] <= threshold:
                k += 1
            end = k - 1
            # Inclusive width: a run from k=start..end spans
            # (end - start + 1) points. Real column gutters on dense
            # multi-column labels measure out at exactly 12pt after the
            # line-endings at the column edges are excluded, so the
            # width test must accept a run of `min_gutter` points, not
            # strictly more (the old >= rejected every real gutter on
            # a 6-column label page by one point).
            # The bucket count rounds each column edge OUTWARD (floor/
            # ceil), reading a gap up to ~2.5pt narrower than it is: dense
            # outserts' real 8.1-8.5pt gutters came out as 6-7pt against
            # a 6.4pt minimum, and whole columns merged on one page but not
            # the next. So the run is also measured exactly, between the
            # text edges that bound it.
            run_width = end - start + 1
            left_text = right_edge_at.get(start - 1)
            right_text = left_edge_at.get(end + 1)
            if left_text is not None and right_text is not None:
                run_width = max(run_width, (right_text - left_text))
            # In a block of a few lines, a run some row crosses is a gutter
            # only as wide as one: in a 3-line block, a full-width line ("...
            # acidosis, including diabetic ketoacidosis [see Warnings and")
            # was cut at the 321pt left free by a short line beside a page
            # number, and its middle read after "Page 7 of 40". (Among many
            # lines one crossing is the exception: a Contents list's wide
            # gutter under its full-width title.)
            crossed = threshold > 0 and any(counts[x] for x in range(start, end + 1))
            too_wide = (
                crossed and run_width > _MAX_CROSSED_GUTTER_WIDTH and total_rows < _FEW_ROWS
            )
            if run_width >= min_gutter and not too_wide:
                gaps.append((x0i + start, x0i + end))
        else:
            k += 1

    if not gaps:
        return [(content_x0, content_x1)]

    # Candidate gutters separated by tiny noise slivers (< 5.0 pt, e.g. a stray
    # punctuation mark or kerning overshoot bridging a single row) belong to the
    # same logical gutter and should not fragment the column into spurious bands.
    merged_gaps: list[tuple[float, float]] = []
    for g_start, g_end in gaps:
        if merged_gaps and g_start - merged_gaps[-1][1] < 5.0:
            merged_gaps[-1] = (merged_gaps[-1][0], g_end)
        else:
            merged_gaps.append((g_start, g_end))

    bands: list[tuple[float, float]] = []
    prev = content_x0
    for gap_start, gap_end in merged_gaps:
        if gap_start > prev:
            bands.append((prev, gap_start))
        prev = gap_end
    if prev < content_x1:
        bands.append((prev, content_x1))

    if not bands:
        return [(content_x0, content_x1)]

    if len(bands) > MAX_COLUMN_BANDS:
        # Pathological over-splitting (e.g. a dense table dominating
        # the page) -- fall back to simple top-to-bottom ordering
        # rather than producing a nonsensical number of columns.
        return [(content_x0, content_x1)]

    return bands


def _split_bands_at_interior_gutters(
    zero_bands: list[tuple[float, float]], tolerant_bands: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Split each zero-crossing band at the tolerant gutters lying wholly
    inside it. Gutters that overlap a zero-crossing gutter (the same
    gutter, merely widened) are ignored, so existing boundaries keep their
    exact edges."""
    if len(tolerant_bands) < 2:
        return zero_bands
    tolerant_gutters = [
        (left[1], right[0]) for left, right in zip(tolerant_bands, tolerant_bands[1:])
    ]
    result: list[tuple[float, float]] = []
    for lo, hi in zero_bands:
        start = lo
        for g_lo, g_hi in tolerant_gutters:
            if g_lo > start and g_hi < hi:
                result.append((start, g_lo))
                start = g_hi
        result.append((start, hi))
    if len(result) > MAX_COLUMN_BANDS:
        return zero_bands
    return result


def _merge_intervals(intervals: list[tuple[float, float]], pad: float = 1.0) -> list[tuple[float, float]]:
    """Merge overlapping/near-touching (x0, x1) intervals, sorted ascending."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for a, b in ordered[1:]:
        if a <= merged[-1][1] + pad:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _group_column_lines(raw_words: list[dict]) -> list[list[dict]]:
    """Cluster words within a single column into visual lines.

    Unlike whole-page gutter detection, words within a column are already
    confined to one vertical stream. Allows words with differing font sizes
    or slight baseline shifts (e.g. 17pt digits alongside 13pt units, or
    tampered/edited text insertions) to join the line if they have vertical
    overlap and do not horizontally collide with existing words in that line.
    """
    if not raw_words:
        return []

    ordered = sorted(raw_words, key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = []

    for w in ordered:
        w_top = w["top"]
        w_bottom = w.get("bottom", w_top + 10.0)
        w_h = max(w_bottom - w_top, 1.0)
        best_line = None
        best_overlap = 0.0

        for line in reversed(lines[-4:]):
            collision = any(
                (min(w["x1"], lw["x1"]) - max(w["x0"], lw["x0"])) > 2.0
                for lw in line
            )
            if collision:
                continue

            avg_top = sum(x["top"] for x in line) / len(line)
            avg_bottom = sum(x.get("bottom", x["top"] + 10.0) for x in line) / len(line)
            l_h = max(avg_bottom - avg_top, 1.0)
            y_overlap = min(w_bottom, avg_bottom) - max(w_top, avg_top)

            if abs(w_top - avg_top) <= LINE_Y_TOLERANCE or (y_overlap > 0 and (y_overlap / min(w_h, l_h)) >= 0.25):
                if y_overlap > best_overlap or best_line is None:
                    best_overlap = y_overlap
                    best_line = line

        if best_line is not None:
            best_line.append(w)
        else:
            lines.append([w])

    return lines


def _group_into_lines(raw_words: list[dict]) -> list[list[dict]]:
    """Cluster pdfplumber word-dicts into lines by vertical position.

    Args:
        raw_words: A list of word-dicts (or Word-like dicts with a
            "top" key), already scoped to whichever set of words
            should be clustered together (a whole page for the global
            gutter measurement, or a single column's words).

    Returns:
        Lines (each a list of word-dicts), ordered top-to-bottom.
        Words within a line are NOT yet sorted -- callers sort by x0.
    """
    if not raw_words:
        return []

    ordered = sorted(raw_words, key=lambda w: w["top"])
    lines: list[list[dict]] = [[ordered[0]]]

    for w in ordered[1:]:
        current_line = lines[-1]
        # Compare against the running average `top` of the current
        # line so tolerance doesn't drift word-by-word.
        line_top = sum(x["top"] for x in current_line) / len(current_line)
        if abs(w["top"] - line_top) <= LINE_Y_TOLERANCE:
            current_line.append(w)
        else:
            w_top = w["top"]
            w_bottom = w.get("bottom", w_top + 10.0)
            w_h = max(w_bottom - w_top, 1.0)
            # A small glyph run touching a word already on this line,
            # on either side of it: a trailing sub/superscript, or a
            # LEADING one -- a section number drawn in its own smaller
            # box ("5." immediately left of "5 Interaction with CYP1A2
            # Inhibitors", 4pt lower and half the height, which is
            # really the heading "5.5 Interaction with CYP1A2
            # Inhibitors"). Split onto a line of its own, such a number
            # loses its heading, so the section is never detected and
            # its whole body is read as part of whatever came before.
            # The vertical-overlap requirement keeps genuine
            # neighboring lines (which do not overlap at all) apart.
            touching = any(
                (0.0 <= (w["x0"] - lw["x1"]) <= 2.5 or 0.0 <= (lw["x0"] - w["x1"]) <= 2.5)
                and (min(w_bottom, lw.get("bottom", lw["top"] + 10.0)) - max(w_top, lw["top"]) >= 0.4 * w_h)
                for lw in current_line
            )
            if touching:
                current_line.append(w)
            else:
                lines.append([w])

    return lines


# ----------------------------------------------------------------------
# Hyphenation-aware "comparison tokens"
# ----------------------------------------------------------------------
# Words stay as the ground truth for coordinates. ComparisonTokens are a
# thin layer on top that merges a hyphenated line-end split ("pharmaco-"
# + "kinetics") back into one logical word ("pharmacokinetics") for
# diffing purposes, while remembering which original Word(s) it came
# from so a click can still land on the right spot(s) on the page.


@dataclass
class ComparisonToken:
    """One unit of comparison: usually one Word, sometimes two merged.

    `text` is EXACT -- casing and punctuation are preserved verbatim
    (never lowercased/stripped) so e.g. "ROPINIROLE" vs "ropinirole"
    still diffs as a mismatch.
    """

    text: str
    source_words: list[Word] = field(default_factory=list)
    # For a word stitched back together across a line break ("placebo-"
    # + "treated"): where in `text` the line-end hyphen stood before it
    # was dropped. Whether that hyphen belongs to the word is decided
    # against the other document (core.diff_engine
    # `_harmonize_line_break_hyphens`).
    line_break_hyphen: Optional[int] = None

    @property
    def page(self) -> Optional[int]:
        return self.source_words[0].page if self.source_words else None

    @property
    def artwork(self) -> bool:
        """In an artwork sheet's box or its note (see `Word.artwork`)."""
        return bool(self.source_words) and all(getattr(w, "artwork", False) for w in self.source_words)

    @property
    def bbox(self) -> Optional[tuple[float, float, float, float]]:
        """Union bounding box across all source words (may span 2 lines)."""
        if not self.source_words:
            return None
        x0 = min(w.x0 for w in self.source_words)
        top = min(w.top for w in self.source_words)
        x1 = max(w.x1 for w in self.source_words)
        bottom = max(w.bottom for w in self.source_words)
        return (x0, top, x1, bottom)


def build_comparison_tokens(words: list[Word]) -> list[ComparisonToken]:
    """Re-stitch hyphenated line-end splits and split numeric/code
    fragments, otherwise 1 Word == 1 token.

    Two DIFFERENT stitch rules, checked in order:

    1. Hyphenated line-end split: a word ends in "-", is followed by
       another word, and that next word starts a new line (different
       page OR `top` more than LINE_Y_TOLERANCE away) -- i.e. the
       hyphen sits at the visual end of a line, not mid-sentence
       punctuation like "well-known thing" on one line. Additionally,
       the two words must belong to the SAME table cell (or both to
       body text), and the continuation must look like a real word
       fragment (starts lowercase or digit): "pharmaco-" + "kinetics"
       is a wrap, "double-" + "When" is not. Both guards are layout-
       independent, so two identical documents can never stitch
       differently -- the phantom "doubleWhen" token cannot form even
       when the table is not detected (borderless/undetected tables).

    2. Split numeric/code fragment (MVP-16): pdfplumber occasionally
       splits ONE code -- a batch number, PIN code, lot number -- into
       multiple word-fragments purely because of a font/size change
       mid-run (a real observed case: the SAME "0-31" batch code came
       back as one word "0-31" from one PDF but as two fragments "0"
       and "-31" from another, independently-typeset PDF of the same
       artwork). Diffing them as separate tokens produces a false
       discrepancy against the other document's single merged token,
       even though the content and reading order are both already
       correct. `_is_code_fragment_split` merges a whole RUN of such
       fragments back into one token -- see that function for the
       narrow, digits/connecting-punctuation-only guard that keeps this
       from ever merging two genuine words.
    """
    tokens: list[ComparisonToken] = []
    i = 0
    n = len(words)

    while i < n:
        word = words[i]
        is_hyphen_split = (
            word.text.endswith("-")
            and len(word.text) > 1  # not a bare "-" bullet/dash
            # ...nor a rule ending a heading line ("------" + "s;" on the
            # next line was read as "-----s;", and the heading's rules
            # were no longer seen to frame it).
            and not _TRAILING_DASH_RULE.search(word.text)
            and i + 1 < n
            and _is_new_line(word, words[i + 1])
            # Only when the next word is in the same table cell (or
            # both are body text) -- never across cell boundaries.
            and word.cell_id == words[i + 1].cell_id
            # And only when the continuation is a plausible word
            # fragment -- never a capitalized word like "When".
            and _plausible_hyphen_continuation(words[i + 1].text)
        )
        if is_hyphen_split:
            nxt = words[i + 1]
            merged_text = word.text[:-1] + nxt.text
            tokens.append(
                ComparisonToken(
                    text=merged_text, source_words=[word, nxt], line_break_hyphen=len(word.text) - 1
                )
            )
            i += 2
            continue

        run = [word]
        j = i + 1
        while j < n and _is_code_fragment_split(run[-1], words[j]):
            run.append(words[j])
            j += 1
        if len(run) > 1:
            tokens.append(
                ComparisonToken(text="".join(w.text for w in run), source_words=run)
            )
            i = j
            continue

        tokens.append(ComparisonToken(text=word.text, source_words=[word]))
        i += 1

    return _drop_dash_rules(tokens)


# A "word" that is nothing but a run of dashes or underscores: the rule a
# Word draft draws either side of a Highlights heading ("--------- DOSAGE
# AND ADMINISTRATION ---------"), which the SPL sets as a plain heading.
_DASH_RULE = re.compile(r"^[-‐-―_]{3,}$")
# ...and the same rule set solid against its heading ("-------DOSAGE",
# "ADMINISTRATION-------"), as an outsert prints it.
_GLUED_DASH_RULE = re.compile(r"^[-‐-―_]{3,}(?=\w)|(?<=\w)[-‐-―_]{3,}$")
_LEADING_DASH_RULE = re.compile(r"^[-‐-―_]{3,}")
_TRAILING_DASH_RULE = re.compile(r"[-‐-―_]{3,}$")
# A heading framed by rules may wrap: "------ DOSAGE FORMS AND" over
# "STRENGTHS ------"; it is a few words long.
_MAX_RULED_HEADING_LINES = 3
_MAX_RULED_HEADING_WORDS = 12


def _drop_dash_rules(tokens: list[ComparisonToken]) -> list[ComparisonToken]:
    """Leave dash rules out of the comparison: they carry no text, and
    compared they turned every Highlights heading of a Word draft into a
    "moved" row and the rules themselves into "deleted" ones. Rules set
    solid against a heading word are taken off it, so "-------DOSAGE"
    and "------- DOSAGE" both compare as "DOSAGE"; a single dash glued
    to a heading word beside a rule ("CHANGES-" before "--------") is
    part of the rule, not of the word.

    Only where rules FRAME a heading -- a rule, a few words, a rule, on
    one line or wrapped over a few -- or a rule stands on a line of its
    own. Dashes anywhere else are text and are compared: "DISCONTINUATION
    OF------" in a boxed warning was read as "OF", and dashes typed into a
    sentence were never reported."""
    framed = _rule_framed_tokens(tokens)
    kept: list[ComparisonToken] = []
    for k, token in enumerate(tokens):
        if k not in framed:
            kept.append(token)
            continue
        if _DASH_RULE.match(token.text):
            continue
        text = _GLUED_DASH_RULE.sub("", token.text)
        if len(text) > 1 and text.endswith("-") and k + 1 < len(tokens) and _DASH_RULE.match(tokens[k + 1].text):
            text = text[:-1]
        if len(text) > 1 and text.startswith("-") and k > 0 and _DASH_RULE.match(tokens[k - 1].text):
            text = text[1:]
        if text != token.text:
            token = ComparisonToken(
                text=text, source_words=token.source_words, line_break_hyphen=token.line_break_hyphen
            )
        kept.append(token)
    return kept


def _rule_framed_tokens(tokens: list[ComparisonToken]) -> set[int]:
    """Indexes of the tokens framed by dash rules (see `_drop_dash_rules`):
    from a token starting with a rule to the next token ending with one,
    a heading's few words apart and on the same line or the next ones. A
    rule alone frames itself. Not the whole line: text printed over the
    end of a heading line (") mg to 4 mg per dose" after "------ DOSAGE
    AND ADMINISTRATION ------") does not unframe it."""
    line_of: list[int] = []
    line = -1
    previous: Optional[Word] = None
    for token in tokens:
        first = token.source_words[0] if token.source_words else None
        if previous is None or first is None or _is_new_line(previous, first):
            line += 1
        line_of.append(line)
        if token.source_words:
            previous = token.source_words[-1]

    def alone_on_line(k: int) -> bool:
        return (k == 0 or line_of[k - 1] != line_of[k]) and (k + 1 == len(tokens) or line_of[k + 1] != line_of[k])

    framed: set[int] = set()
    k = 0
    while k < len(tokens):
        if not _LEADING_DASH_RULE.match(tokens[k].text):
            k += 1
            continue
        end = None
        if not _DASH_RULE.match(tokens[k].text) and _TRAILING_DASH_RULE.search(tokens[k].text):
            framed.add(k)  # "-----CONTRAINDICATIONS----", one word
            k += 1
            continue
        for j in range(k + 1, min(len(tokens), k + _MAX_RULED_HEADING_WORDS + 2)):
            if line_of[j] - line_of[k] >= _MAX_RULED_HEADING_LINES:
                break
            if _TRAILING_DASH_RULE.search(tokens[j].text):
                end = j
                break
            if _LEADING_DASH_RULE.match(tokens[j].text):
                break  # another heading opens before this one closed
        if end is None and _DASH_RULE.match(tokens[k].text) and alone_on_line(k):
            end = k  # a rule on a line of its own
        if end is None:
            k += 1
            continue
        framed.update(range(k, end + 1))
        k = end + 1
    return framed


def _is_new_line(word: Word, next_word: Word) -> bool:
    if word.page != next_word.page:
        return True
    return abs(next_word.top - word.top) > LINE_Y_TOLERANCE


def _plausible_hyphen_continuation(text: str) -> bool:
    """True if ``text`` could continue a line-end hyphenated word.

    A genuine hyphenation wrap continues with the tail of the same
    word, which in practice is lowercase or a digit ("kinetics",
    "2-D") -- never a capitalized word. This is a purely textual
    check, so it holds regardless of where lines happen to break or
    whether the surrounding region was detected as a table: "double-"
    followed by "When" can never stitch into "doubleWhen".

    Nor is "and"/"or"/"to"/"nor" a continuation: "age-" at the end of a
    line followed by "and sex-matched" is a suspended compound (age- and
    sex-matched), which stitched into the non-word "ageand".
    """
    return (
        bool(text)
        and (text[0].islower() or text[0].isdigit())
        and text.rstrip(",;:.") not in _SUSPENDED_HYPHEN_WORDS
    )


_SUSPENDED_HYPHEN_WORDS = frozenset({"and", "or", "to", "nor"})


# A gap this small (or a hair of overlap, from font-metric rounding)
# between two adjacent numeric/code word-fragments on the SAME line
# means they are visually touching -- ordinary inter-word spacing is
# several points wide even at small font sizes, so a gap this tight is
# a pdfplumber tokenization artifact, not a real word boundary. See
# _is_code_fragment_split.
_CODE_FRAGMENT_MIN_GAP = -0.5
_CODE_FRAGMENT_MAX_GAP = 1.0

# Characters allowed in a "code fragment" -- digits and the punctuation
# that commonly glues pieces of a batch/lot/PIN code together. NOT
# letters: this is what keeps the rule from ever merging two genuine
# words (see _is_code_fragment_split).
_CODE_FRAGMENT_PUNCTUATION = frozenset("-/.:,")


def _is_code_fragment_text(text: str) -> bool:
    return bool(text) and all(ch.isdigit() or ch in _CODE_FRAGMENT_PUNCTUATION for ch in text)


def _is_code_fragment_split(word: Word, next_word: Word) -> bool:
    """True if ``word``/``next_word`` are almost certainly ONE
    numeric/alphanumeric code that pdfplumber split into two word-
    fragments purely because of a font/size change mid-run -- not two
    genuinely separate words.

    Deliberately narrow: BOTH fragments must contain no actual letters
    at all (only digits and connecting punctuation like "-", "/", ".",
    ":", ","), so this can only ever merge fragments of a code/number,
    never two genuine words -- there is no font/kerning glitch that
    should make "double" and "When" merge into one token. The two
    fragments must also be on the SAME visual line (this is the
    opposite situation from the hyphen line-wrap rule above, which
    only fires ACROSS a line break) with essentially zero gap between
    them, and in the same table cell (or both body text).
    """
    if word.page != next_word.page:
        return False
    if word.cell_id != next_word.cell_id:
        return False
    y_overlap = min(word.bottom, next_word.bottom) - max(word.top, next_word.top)
    if y_overlap <= 0 and abs(next_word.top - word.top) > LINE_Y_TOLERANCE:
        return False
    gap = next_word.x0 - word.x1
    if not (_CODE_FRAGMENT_MIN_GAP <= gap <= _CODE_FRAGMENT_MAX_GAP):
        return False
    return _is_code_fragment_text(word.text) and _is_code_fragment_text(next_word.text)



# ---------------------------------------------------------------------------
# Scan detection
# ---------------------------------------------------------------------------
#
# A single, OCR-independent check: does a PDF actually have a real,
# extractable digital text layer at all. This app compares digital
# text only (no OCR), so a scanned/image-only PDF is a genuine input
# error worth catching with a clear message rather than silently
# extracting zero words and producing a meaningless empty diff.

# A page is judged "scanned" (no usable digital text) when its
# extracted text has fewer non-whitespace characters than this. Not
# zero: a scanner's own software occasionally burns in a thin OCR text
# layer that's mostly garbage (a handful of misrecognized characters),
# and a handful of stray characters shouldn't count as "has real text".
MIN_DIGITAL_TEXT_CHARS = 20


def page_has_text_layer(page, min_chars: int = MIN_DIGITAL_TEXT_CHARS) -> bool:
    """True if a single pdfplumber ``page`` has a real, extractable
    digital text layer -- i.e. this page has something to compare, as
    opposed to it being a flattened scan/photo with no underlying text
    objects at all."""
    text = page.extract_text() or ""
    non_space_chars = sum(1 for ch in text if not ch.isspace())
    return non_space_chars >= min_chars


def is_scanned_pdf(doc_path: str) -> bool:
    """True if NONE of ``doc_path``'s pages have a usable digital text
    layer -- i.e. this PDF has nothing this app can extract and
    compare as text.

    Deliberately whole-document: a PDF with a MIX of digital and
    scanned pages still has usable text on some pages, so it is
    reported as NOT fully scanned here (Text extraction will still
    find at least some content); callers that need finer-grained
    control should call ``page_has_text_layer`` per page instead.

    Args:
        doc_path: Path to a PDF file.

    Returns:
        True if every page lacks a usable text layer. False if at
        least one page has real digital text, OR if the file can't be
        opened as a PDF at all (an unreadable/corrupt file is reported
        False here so it falls through to extract_words's own, more
        specific error rather than being silently misreported as
        "scanned").
    """
    try:
        with fitz.open(doc_path) as doc:
            if not doc.page_count:
                return False
            return not any(
                sum(1 for ch in page.get_text() if not ch.isspace()) >= MIN_DIGITAL_TEXT_CHARS
                for page in doc
            )
    except Exception:
        logger.warning("is_scanned_pdf: could not open %s as a PDF", doc_path, exc_info=True)
        return False
