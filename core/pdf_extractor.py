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
# Width (pt) of the horizontal buckets used to skip glyphs with no larger
# glyph close by. Neighbouring buckets are checked too, so a base up to
# this far away is always seen (glyphs touch within _SCRIPT_MAX_GAP).
_SIZE_BUCKET_WIDTH = 40.0


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

    def neighbors(c: dict, left: bool) -> list[dict]:
        index, edge = (by_x1, round(c["x0"])) if left else (by_x0, round(c["x1"]))
        c_band = band(c)
        found = []
        for x in range(edge - 2, edge + 3):
            for b in (c_band - 1, c_band, c_band + 1):
                for d in index.get((x, b), ()):
                    gap = c["x0"] - d["x1"] if left else d["x0"] - c["x1"]
                    if d is not c and -_SCRIPT_MAX_GAP <= gap <= _SCRIPT_MAX_GAP and (
                        d["top"] < c["bottom"] and c["top"] < d["bottom"]
                    ):
                        found.append(d)
        return found

    def baseline(c: dict) -> float:
        return c["matrix"][5]  # PDF space, y up

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
        candidates = neighbors(c, left=True) + neighbors(c, left=False) if can_have_base else []
        for d in candidates:
            if (
                d["size"] * _SCRIPT_MIN_SIZE_RATIO <= size <= d["size"] * _SCRIPT_MAX_SIZE_RATIO
                and abs(baseline(c) - baseline(d)) <= d["size"] * _SCRIPT_MAX_RISE
            ):
                base = d
                break
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


def extract_words(pdf_path: str, first_page: int = 1, last_page: Optional[int] = None) -> list[Word]:
    """Extract every word in a PDF, in column-aware reading order.

    Args:
        pdf_path: Path to the PDF file.
        first_page, last_page: 1-indexed, inclusive page range (default:
            the whole document). Pages are processed independently, so
            ranges can be extracted in parallel and concatenated.

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
            # Filter words outside the page canvas (e.g. pasteboard artifacts outside trim box)
            canvas_words = [
                w for w in raw_words
                if w["x1"] > (page_x0 - 5.0)
                and w["x0"] < (page_x0 + pw + 5.0)
                and w["bottom"] > (page_top0 - 5.0)
                and w["top"] < (page_top0 + ph + 5.0)
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
            upright_words = [w for w in filtered_raw_words if w.get("upright", True)]
            # Tables only decide how the page's upright words are read. A
            # page without any (a scan, a blank page, artwork) skips the
            # search -- which reads every vector path on the page, a
            # million of them on an outlined scan.
            tables = _detect_page_tables(page) if upright_words else []
            # One RTL decision per page -- stable across renderings.
            page_rtl = _page_is_rtl(filtered_raw_words)
            ordered_words = _order_page_words(upright_words, tables, rtl=page_rtl, page=page)
            rotated_chars = [c for c in page.chars if not c.get("upright", True)]
            ordered_words.extend(_ordered_rotated_words(rotated_chars))
            for w in ordered_words:
                raw_text = w["text"]
                # pdfplumber merges chars ascending by x0, which reverses
                # the letters inside RTL words. Detect and undo for RTL words.
                is_rtl_word = _text_is_rtl(raw_text)
                if is_rtl_word:
                    raw_text = _fix_rtl_word_chars(raw_text)

                all_words.append(
                    Word(
                        text=_normalize_word_text(raw_text, mirror_brackets=is_rtl_word),
                        page=page_idx,
                        x0=w["x0"] - page_x0,
                        top=w["top"] - page_top0,
                        x1=w["x1"] - page_x0,
                        bottom=w["bottom"] - page_top0,
                        cell_id=w.get("cell_id"),
                    )
                )
    return all_words


def _order_page_words(
    raw_words: list[dict],
    tables: Optional[list[dict]] = None,
    *,
    rtl: bool = False,
    page: Optional[Any] = None,
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

    cell_ids = _assign_words_to_cells(raw_words, tables)
    real_table_idxs = {
        t_idx
        for t_idx in range(len(tables))
        if _looks_like_real_table(t_idx, cell_ids)
        and not _cuts_through_words(tables[t_idx], raw_words)
    }
    cell_ids = {w_idx: cid for w_idx, cid in cell_ids.items() if cid[0] in real_table_idxs}

    # Tag the word dicts themselves so extract_words() can carry each
    # word's cell membership onto the Word objects (the hyphen-stitch
    # guard in build_comparison_tokens depends on it). Body words are
    # left without the key -> cell_id None.
    for w_idx, cid in cell_ids.items():
        raw_words[w_idx]["cell_id"] = cid

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
                        t_idx, entries, [(content_x0, content_x1)], cell_ids, tables[t_idx]["bbox"], rtl=rtl
                    )
                )
        return [w for _, _, words in sorted(table_units, key=lambda u: u[1]) for w in words]

    rows = sorted(_group_into_lines(body_words), key=_line_top)
    page_bands = _detect_column_bands(
        rows, content_x0, content_x1, min_gutter=_adaptive_min_gutter(rows)
    )
    page_bands = _merge_narrow_column_bands(page_bands, min_width=25.0)
    is_page_cols = len(page_bands) >= 2 and _is_robust_page_columns(page_bands, content_x0, content_x1)
    if not is_page_cols:
        page_panels = _detect_page_panels(rows, content_x0, content_x1)
        if page_panels is not None:
            page_bands = page_panels
            is_page_cols = True

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
        table_units.extend(
            _table_units_for(t_idx, entries, bands, cell_ids, t_box, band_idx=best_b, rtl=rtl)
        )

    all_units = body_units + table_units
    ordered: list[dict] = []
    for band_idx in range(len(bands)):
        band_units = sorted(
            (u for u in all_units if u[0] == band_idx),
            key=lambda u: u[1],
        )
        for _, _, words in band_units:
            ordered.extend(words)
    return ordered


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
    blocks = _reconstruct_rotated_blocks(rotated_chars)
    blocks.sort(key=lambda block: min(w["x0"] for w in block))
    words: list[dict] = []
    for block in blocks:
        words.extend(block)
    return words


def _reconstruct_rotated_blocks(chars: list[dict]) -> list[list[dict]]:
    """Group non-upright characters by rotation angle, then reconstruct
    each angle's characters into spatially-coherent blocks of words
    (see ``_words_from_rotated_group``).
    """
    by_rotation: dict[tuple, list[dict]] = {}
    for ch in chars:
        matrix = ch.get("matrix")
        if not matrix:
            continue
        by_rotation.setdefault(_rotation_bucket(matrix), []).append(dict(ch))

    blocks: list[list[dict]] = []
    for (a, b, c, d), group in by_rotation.items():
        blocks.extend(_words_from_rotated_group(group, a, b, c, d))
    return blocks


def _words_from_rotated_group(
    chars: list[dict], a: float, b: float, c: float, d: float
) -> list[list[dict]]:
    """One rotation angle's characters -> blocks of correctly-ordered
    word-dicts.

    Projects each character's position onto the matrix's own advance
    direction (a, b) and perpendicular direction (c, d) -- see the
    MVP-14 module note -- to cluster characters into lines, order
    characters within a line, and order lines relative to each other,
    all without assuming horizontal text.
    """
    for ch in chars:
        ch["_perp"] = -c * ch["x0"] + d * ch["top"]
        ch["_adv"] = a * ch["x0"] - b * ch["top"]

    ordered = sorted(chars, key=lambda ch: ch["_perp"])
    lines: list[list[dict]] = [[ordered[0]]]
    for ch in ordered[1:]:
        line_perp = sum(x["_perp"] for x in lines[-1]) / len(lines[-1])
        if abs(ch["_perp"] - line_perp) <= ROTATED_LINE_TOLERANCE:
            lines[-1].append(ch)
        else:
            lines.append([ch])

    blocks: list[list[dict]] = []
    prev_perp: Optional[float] = None
    for line in lines:
        line_perp = sum(ch["_perp"] for ch in line) / len(line)
        if prev_perp is not None and abs(line_perp - prev_perp) > ROTATED_BLOCK_GAP:
            blocks.append([])
        if not blocks:
            blocks.append([])
        blocks[-1].extend(_words_from_rotated_line(line))
        prev_perp = line_perp
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
    words: list[dict] = []
    current: list[dict] = []
    for ch in line:
        if ch["text"].isspace():
            if current:
                words.append(_merge_rotated_chars(current))
                current = []
        else:
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
) -> dict[int, tuple[int, int, int]]:
    """Map each word whose center falls in a table to (table, row, col)."""
    cell_ids: dict[int, tuple[int, int, int]] = {}
    for t_idx, table in enumerate(tables):
        t_x0, t_top, t_x1, t_bottom = table["bbox"]
        cell_index = [
            (r, c, cell)
            for r, row in enumerate(table["cells"])
            for c, cell in enumerate(row)
            if cell is not None
        ]
        for w_idx, w in enumerate(raw_words):
            if w_idx in cell_ids:
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
    crossing = sum(
        1
        for word in inside
        if any(
            word["x0"] < edge - _STRADDLE_TOLERANCE and word["x1"] > edge + _STRADDLE_TOLERANCE
            for edge in edges
        )
    )
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
                    if is_likely_header_line(prefix_text):
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
    groups: list[list[list[dict]]] = [[]]
    for row in zone_rows:
        has_label = any(
            label_band[0] <= (w["x0"] + w["x1"]) / 2.0 <= label_band[1] for w in row
        )
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
                if label_band[0] <= mid_x <= label_band[1]:
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

        for col_band, y_lo, y_hi, col_idx in streams:
            col_words = [
                w for w in body_words
                if _band_index_for((w["x0"] + w["x1"]) / 2.0, page_bands) == col_idx
                and y_lo <= (w["top"] + w["bottom"]) / 2.0 < y_hi
            ]
            if not col_words:
                continue
            col_rows = sorted(_group_column_lines(col_words), key=_line_top)
            col_zones = _split_into_zones(col_rows)
            for zone_rows in col_zones:
                zone_words = [w for row in zone_rows for w in row]
                zone_x0 = min(w["x0"] for w in zone_words)
                zone_x1 = max(w["x1"] for w in zone_words)
                band_offset = len(all_bands)
                if len(zone_rows) < 3:
                    all_bands.append((zone_x0, zone_x1))
                    for line in zone_rows:
                        all_units.append((band_offset, _line_top(line), _sort_line_words(line, rtl=rtl)))
                    continue
                zone_bands = _detect_column_bands(
                    zone_rows, zone_x0, zone_x1, min_gutter=_adaptive_min_gutter(zone_rows)
                )
                band_offset = len(all_bands)
                if len(zone_bands) >= 2 and not _bands_are_column_flow(zone_bands):
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
                        for line in _group_column_lines(words_in_band):
                            all_units.append(
                                (band_offset + out_idx, _line_top(line), _sort_line_words(line, rtl=rtl))
                            )
                else:
                    all_bands.append((zone_x0, zone_x1))
                    for line in zone_rows:
                        all_units.append((band_offset, _line_top(line), _sort_line_words(line, rtl=rtl)))

        return all_units, all_bands

    zones = _split_into_zones(rows)
    all_units: list[tuple[int, float, list[dict]]] = []
    all_bands: list[tuple[float, float]] = []
    for zone_rows in zones:
        zone_words = [w for row in zone_rows for w in row]
        zone_x0 = min(w["x0"] for w in zone_words)
        zone_x1 = max(w["x1"] for w in zone_words)
        band_offset = len(all_bands)
        if len(zone_rows) < 3:
            all_bands.append((zone_x0, zone_x1))
            for line in zone_rows:
                all_units.append((band_offset, _line_top(line), _sort_line_words(line, rtl=rtl)))
            continue
        zone_bands = _detect_column_bands(
            zone_rows, zone_x0, zone_x1, min_gutter=_adaptive_min_gutter(zone_rows)
        )
        band_offset = len(all_bands)

        if len(zone_bands) >= 2 and not _bands_are_column_flow(zone_bands):
            all_bands.append((zone_x0, zone_x1))
            for line_idx, line in enumerate(
                _order_label_value_zone(zone_rows, zone_bands, rtl=rtl)
            ):
                all_units.append((band_offset, float(line_idx), line))
            continue

        if len(zone_bands) <= 1:
            all_bands.append((zone_x0, zone_x1))
            for line in zone_rows:
                all_units.append((band_offset, _line_top(line), _sort_line_words(line, rtl=rtl)))
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
            for line in _group_column_lines(words_in_band):
                all_units.append(
                    (band_offset + out_idx, _line_top(line), _sort_line_words(line, rtl=rtl))
                )

    return all_units, all_bands


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
    words_by_col: dict[int, list[dict]] = {}
    non_header_by_col: dict[int, list[dict]] = {}
    for w_idx, w in entries:
        r, c = cell_ids[w_idx][1:]
        by_cell.setdefault((r, c), []).append(w)
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

    units: list[tuple[int, float, list[dict]]] = []
    for (r, c) in sorted(by_cell):
        words = by_cell[(r, c)]
        sub_bands = col_sub_bands.get(c, [])
        if len(sub_bands) <= 1 or not words:
            cell_words: list[dict] = []
            for line in _group_into_lines(words):
                cell_words.extend(_sort_line_words(line, rtl=rtl))
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
        sort_key = row_tops.get(r, min(w["top"] for w in cell_words)) + col_offset
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
    return min(25.0, max(MIN_GUTTER_WIDTH_FLOOR, typical_gap * GUTTER_WIDTH_MULTIPLIER))


def _detect_column_bands(
    rows: list[list[dict]],
    content_x0: float,
    content_x1: float,
    min_gutter: float = MIN_COLUMN_GUTTER_WIDTH,
    tolerance_fraction: float = COLUMN_GUTTER_ROW_TOLERANCE_FRACTION,
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
    for row in rows:
        intervals = _merge_intervals([(w["x0"], w["x1"]) for w in row])
        for a, b in intervals:
            lo = max(0, int(math.floor(a)) - x0i)
            hi = min(width, int(math.ceil(b)) - x0i)
            if hi < lo:
                hi = lo
            diff[lo] += 1
            diff[hi + 1] -= 1

    counts = [0] * (width + 1)
    running = 0
    for k in range(width + 1):
        running += diff[k]
        counts[k] = running

    if tolerance_fraction > 0:
        zero_bands = _detect_column_bands(
            rows, content_x0, content_x1, min_gutter=min_gutter, tolerance_fraction=0.0
        )
        if len(zero_bands) >= 2:
            return zero_bands

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
            if (end - start + 1) >= min_gutter:
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

    @property
    def page(self) -> Optional[int]:
        return self.source_words[0].page if self.source_words else None

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
            tokens.append(ComparisonToken(text=merged_text, source_words=[word, nxt]))
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

    return tokens


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
    """
    return bool(text) and (text[0].islower() or text[0].isdigit())


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
