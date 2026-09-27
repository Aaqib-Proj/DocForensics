"""Section-aware comparison pipeline (core/section_diff.py).

Three standalone, fully-typed building blocks that compare two PDFs at
the SECTION level instead of the raw word-stream level:

    extract_reading_order(pdf_path) -> blocks in visual column order
    build_section_map(blocks)       -> {section header: [blocks...]}
    diff_section_maps(app, susp)    -> keyed section diffs (diff-match-patch)

Why this pipeline exists
========================
A linear word-stream diff assumes Block A always precedes Block B. Real
production adaptation (full manuals condensed into pocket guides,
folding-carton inserts, FDA-vs-EMA leaflets) reorders whole sections,
so a naive stream diff reports the moved section as a critical
"removed + added" cascade -- or worse, pairs section 2 of one document
against section 5 of the other because their *bodies* are interleaved
across columns during extraction. This module fixes that by:

1. reading each page in COLUMN-MAJOR order (top-to-bottom within a
   column, columns left-to-right) so multi-column layouts extract in
   the order a human reads them;
2. chunking the block stream into SEMANTIC SECTIONS keyed by their
   numbered headers ("2 DOSAGE AND ADMINISTRATION"), so each section
   is a first-class, addressable unit;
3. DIFFING BY KEY: a section is only ever compared with the section
   carrying the same header on the other side -- never with an
   unrelated section -- and sections present on only one side are
   reported as deleted/inserted without running any text diff.

The word-level diff engine (core/diff_engine.py) uses
`build_section_map` for its chunking and keeps its own finer-grained
word diffs with coordinates; this module is the reference block-level
implementation of the same pipeline and the public API for tools that
only need text-level results.
"""

from __future__ import annotations

import re
from typing import Optional

import fitz  # PyMuPDF

from diff_match_patch import diff_match_patch

import unicodedata

# --- Header detection ---------------------------------------------------
# The spec's heading regex: a number (optionally dotted, e.g. "2.1")
# followed by an all-caps title run. Combined with the heuristics below
# (number-alone lines, wrapped titles, subsections) it anchors sections.
_SPEC_HEADER_RE = re.compile(r"^[0-9\u0660-\u0669]+(\.[0-9\u0660-\u0669]+)?\s+[A-Z\s]{3,}")

# A line whose FIRST token is a bare number ("1", "1)", "1.") -- the FDA
# style that prints the number on its own line with the all-caps title
# on the line(s) directly below it.
_NUMBERED_HEADER_RE = re.compile(r"^[0-9\u0660-\u0669]{1,3}[.)\-:]?$")

# A subsection header ("2.1 Dosage in adults", "5.3 Abnormal Thinking"):
# a N-dot-N token at the start of a line followed by a capitalized word.
# Subsection chunking matters because leaflets interleave sections
# ACROSS columns, so a top-level chunk can absorb another section's
# overflow that has its own numbered subsection.
_SUBSECTION_HEADER_RE = re.compile(r"^[0-9\u0660-\u0669]{1,3}\.[0-9\u0660-\u0669]{1,3}$")

# A header token whose number and title text are fused with zero space
# (e.g. "1INDICATIONS", "2.1Dosage", "5WARNINGS").
_FUSED_NUMBER_HEADER_RE = re.compile(
    r"^([0-9\u0660-\u0669]{1,3}(?:\.[0-9\u0660-\u0669]{1,3})?)([A-Za-z\u0600-\u06FF].*)$"
)

# Low-significance words that belong to a subsection title even though
# they are lowercase ("2.1 Dosage in adults" keeps the "in").
_SUBSECTION_CONNECTORS = {
    "in", "of", "and", "for", "with", "to", "at", "by", "on", "or",
}

# A token that may appear inside an all-caps header/title (digits,
# punctuation and ampersands are common: "1 INDICATIONS AND USAGE",
# "FD&C BLUE NO. 2", "5.2 WARNINGS (5.2)").
_CAPS_TOKEN_RE = re.compile(r"^[A-Z0-9][A-Z0-9&'()/,.:\-]*$")

# MVP-19: an UNNUMBERED all-caps header line -- e.g. a credit-card
# Cardmember Agreement's "CARDMEMBER AGREEMENT", "INTEREST CHARGES",
# "ARBITRATION" running heads, which never carry a leading section
# number the way FDA/EMA-style documents do. Deliberately stricter
# than _CAPS_TOKEN_RE above (no digits, no embedded dots) so this can
# never mistake a document control code ("CM.TL24K.LIN.0215" is
# nominally "all-caps" too, but its tokens mix digits and bare dots
# into what a real title's words never do) for a header. Every token
# must be PURE LETTERS, optionally with an internal apostrophe/
# ampersand/hyphen ("DON'T", "FD&C", "NON-REFUNDABLE").
_UNNUMBERED_HEADER_WORD_RE = re.compile(r"^[A-Z\u00C0-\u024F][A-Z\u00C0-\u024F'&-]*$")

# A genuine header is a short title, not a full sentence -- caps this
# many words so a standalone all-caps WARNING PARAGRAPH (legal boiler-
# plate sometimes shouts a whole sentence) isn't mistaken for one.
_MAX_UNNUMBERED_HEADER_WORDS = 6

# Total alphabetic characters the line must have -- low enough that a
# genuine short single-word header ("FEES") still qualifies, but high
# enough to exclude a stray short fragment (a lone "OR", an
# initial-like "A") from ever counting as a header on its own.
_MIN_UNNUMBERED_HEADER_LETTERS = 4

# A date ("12/4/13") or time-of-day ("2:39") stamp -- a print proof's
# page footer, never a real section title -- but one that otherwise
# satisfies `_is_caps_line`'s plain per-token character-class check
# (digits, "/" and ":" are all allowed in a caps TOKEN, since a real
# title legitimately uses them too: "FD&C BLUE NO. 2", "5.2 WARNINGS
# (5.2)"). A real observed case: a bare page-number line ("1") is a
# valid NUMBERED_HEADER_RE start, and a following footer stamp
# ("12/4/13 2:39 PM") satisfying `_is_caps_line` let the wrap-
# continuation logic absorb it -- and then the REAL header text after
# it -- into one garbled "title" for that stray page number.
_DATE_OR_TIME_TOKEN_RE = re.compile(r"^\d{1,4}[/:.]\d{1,4}([/:.]\d{1,4})?$")

_NON_LATIN_SENTENCE_END = {".", "!", "?", "؟", "،", ",", ";", "؛"}
_BULLET_CHARS = {"", "•", "-", "*", "–", "—", "\uf0b7"}
_DOSE_UNITS = {"مجم", "مل", "mg", "ml", "mcg", "g", "kg", "mmol", "iu", "ppm", "%"}

_ARABIC_HEADER_KEYWORDS = {
    "تكوين", "التركيب", "وصف", "الوصف", "شكل الجرعة", "الشكل الصيدلاني", "الشكل",
    "المؤشرات العلاجية", "دواعي الاستعمال", "دواعي", "علم الصيدلة وطريقة الادارة",
    "علم الصدلية وطريقة الادار ة", "علم الصيدلة", "الجرعة وطريقة الاستعمال",
    "الجرعة وطريقة الإعطاء", "الجرعة", "موانع الاستعمال", "موانع", "موان ع", "ع موان",
    "تحذيرات خاصة واحتياطات الاستخدام", "تحذيرات خاصة", "تحذيرات",
    "تحذيرات الخاصة واحتياطات الازمة للاستعما ل", "تحذيرات الخاصة واحتياطات الازمة للاستعمال",
    "التفاعلات الدوائية", "التفاعل مع المنتجات الطبية", "التفاعل مع المنتجات الطبية الأخرى وغيرها من أشكال التفاع ل",
    "الحمل والرضاعة", "الحمل والرضاع ة", "الحمل", "الرضاعة", "الرضاع ة",
    "تأثيرات على القدرة على القيادة واستخدام الآلات", "تأثيرات على القدرة على القيادة واستخدام الآلا ت",
    "الآثار الجانبية", "آثار غير مرغوبة", "آثار غير مرغوب ة", "التأثيرات الجانبية",
    "الجرعة الزائدة", "جرعة زائدة", "جرعة زائد ة", "فرط الجرعة",
    "الخصائص الدوائية", "الخصائص الدوائي ة", "الخصائص الصيدلانية", "الخواص الدوائية",
    "قائمة السواغات", "المكونات غير الفعالة", "عدم التوافق", "احتياطات خاصة للتخزين",
    "حالة التخزين", "ظروف التخزين", "طبيعة الحاوية ومحتوياتها", "رقم النسخة",
    "تاريخ آخر مراجعة", "تاريخ مراجعة النص", "صنع في", "المصنع", "مصنع"
}

_NON_LATIN_BODY_START_WORDS = {
    "وفي", "ولم", "ويمكن", "إذا", "وقد", "ان", "أما", "وهذا", "كما", "حيث",
    "عن", "مع", "إلى", "في", "من", "على", "له", "لها", "به", "بها", "تجنب",
    "يجب", "لا", "فبالجرعة", "والعلاجية", "وضرب", "ويستخدم", "فاساكا", "بيمبالي",
    "يشتيمادهو", "كولينجانا", "أبديل", "أبديل-أتش", "للاستخدام", "لذلك", "أعمارهم", "فليس"
}


def _is_rtl_text(text: str) -> bool:
    for ch in text:
        bidi = unicodedata.bidirectional(ch)
        if bidi in {"R", "AL"}:
            return True
    return False


def _is_caps_continuation_line(text: str) -> bool:
    """Like `_is_caps_line`, but additionally rejects a line dominated
    by a date/timestamp-shaped token (see `_DATE_OR_TIME_TOKEN_RE`
    above). Used ONLY when deciding whether to ABSORB a line into a
    numbered header's wrapped title (`build_section_map`'s two
    wrap-consumption loops) -- never changes whether a line IS a
    header by itself, and never affects `_is_caps_line`'s other
    callers (a same-line title check, and the caps-token character
    class other detection rules already depend on).
    """
    tokens = text.split()
    if any(_DATE_OR_TIME_TOKEN_RE.match(t) for t in tokens):
        return False
    return _is_caps_line(text)


def _looks_like_unnumbered_header(line_text: str) -> bool:
    """True if ``line_text`` alone satisfies the unnumbered header shape.

    Supports both Latin all-caps headers (e.g. CARDMEMBER AGREEMENT)
    and non-Latin / Arabic section titles (e.g. تكوين, الخصائص الصيدلانية,
    وصف, موانع, etc.).
    """
    tokens = line_text.strip().split()
    if not tokens:
        return False

    if tokens[0] in _BULLET_CHARS or (len(tokens[0]) > 0 and tokens[0][0] in _BULLET_CHARS):
        return False

    # Latin all-caps unnumbered headers
    if (
        1 <= len(tokens) <= _MAX_UNNUMBERED_HEADER_WORDS
        and all(_UNNUMBERED_HEADER_WORD_RE.match(t) for t in tokens)
        and sum(ch.isalpha() for ch in line_text) >= _MIN_UNNUMBERED_HEADER_LETTERS
    ):
        return True

    # Latin title-case unnumbered headers ending with a colon (e.g. "Your Billing Rights:")
    clean = line_text.strip()
    if clean.endswith(":"):
        head_tokens = clean[:-1].split()
        param_words = {"auc", "cmax", "pk", "ci", "cl", "crcl", "egfr", "ic50", "ec50", "side", "tip", "disease", "impairment", "weight", "caution", "warning", "note", "notice", "manufactured", "distributed", "marketed", "revised", "date", "prepared"}
        if any(t.lower().strip("():") in param_words for t in head_tokens):
            return False
        if any(c in clean for c in "<>=†‡§*"):
            return False
        if 1 <= len(head_tokens) <= _MAX_UNNUMBERED_HEADER_WORDS:
            all_caps_words = True
            for t in head_tokens:
                w = "".join(c for c in t if c.isalnum())
                if not w:
                    continue
                if w.lower() in _SUBSECTION_CONNECTORS:
                    continue
                if not w[0].isupper():
                    all_caps_words = False
                    break
            if all_caps_words and sum(ch.isalpha() for ch in clean) >= _MIN_UNNUMBERED_HEADER_LETTERS:
                return True

    # Non-Latin / RTL headers (Arabic, Hebrew, etc.)
    if _is_rtl_text(line_text):
        clean = line_text.strip().rstrip(" :：-").strip()
        norm = "".join(ch for ch in clean if ch.isalnum() or ch.isspace())
        for kw in _ARABIC_HEADER_KEYWORDS:
            kw_norm = "".join(ch for ch in kw if ch.isalnum() or ch.isspace())
            if norm == kw_norm:
                return True
            if norm.startswith(kw_norm) and len(norm) <= len(kw_norm) + 15:
                return True
        if 1 <= len(tokens) <= 3 and len(clean) <= 25:
            raw_stripped = line_text.strip()
            if raw_stripped.startswith((".", "،", "؛", ",", "!", "?", "؟", "-", ":")) or raw_stripped.endswith((".", "،", "؛", ",", "!", "?", "؟")):
                return False
            if not any(tokens[0].startswith(w) or tokens[-1].startswith(w) for w in _NON_LATIN_BODY_START_WORDS):
                if sum(ch.isalpha() for ch in clean) >= 3:
                    return True

    return False


def is_likely_header_line(line_text: str) -> bool:
    """Cheap, single-line "does this look like SOME kind of section
    header" check -- numbered, subsection, spec-style, or unnumbered
    (see the four detection rules in `build_section_map`'s docstring).

    This is a public, reusable EXPORT of the same shape checks
    `build_section_map` uses, deliberately WITHOUT its multi-line wrap
    absorption or unnumbered-neighbor guard: those exist to build a
    precise, final {header: body} map, which is unnecessary work for a
    caller that only wants a quick per-line "is this likely a header"
    signal (MVP-20's caller: `core.pdf_extractor` uses this to find
    where a PAGE's column structure is likely to change -- see that
    module's `_split_into_zones` -- since a page that stacks
    differently-columned sections, e.g. a single-column clause
    followed by a 2-column glossary followed by a 3-column arbitration
    clause, needs column-gutter detection run separately per section:
    a single-column section's lines always cross whatever narrow
    gutter a multi-column section further down the SAME page actually
    needs, so detecting gutters once for the whole page can reject a
    real gutter that is completely clean within its own section).
    An occasional false positive/negative here only costs an extra or
    missing zone boundary, not an incorrect section pairing, so the
    lower precision than `build_section_map`'s own detection is an
    acceptable, deliberate trade-off.

    MVP-21 fix: the numbered-header branch below used to accept ANY
    line whose first token matched `_NUMBERED_HEADER_RE` -- a bare
    1-3 digit number, optionally with a trailing "." or ")" -- with NO
    further validation, unlike `build_section_map`'s own numbered
    branch (which only accepts one when the rest of the line, or a
    following line, is actually caps-styled: `has_caps_words`/
    `j > i + 1` below). That is far too loose for a zone-splitting
    caller applied to EVERY row on a page with no neighbor guard:
    a numbered LIST ITEM ("1. Within 30 days of receiving your
    letter, we must tell you...") and a bare DOSE NUMBER ("150 mg",
    "75 mg" -- extremely common on pharma labels) both start with
    exactly this shape and are not headers at all. Two real reported
    documents (a credit-card agreement dense with numbered clauses,
    and an Arabic drug label's dosage table) each triggered a zone
    boundary on nearly EVERY line, fragmenting the page into zones of
    1-2 rows each -- and with that little data, `_adaptive_min_gutter`
    can't measure the zone's own word spacing (needs >= 5 gap
    samples) and falls back to a fixed threshold, which then
    misreads an ordinary wide gap WITHIN one line (e.g. before a
    parenthetical) as a column gutter, splitting that single line's
    own words into fake columns that get stitched back in the wrong
    relative order against the next zone. The result was severe,
    widespread word-order corruption -- not just an extra zone
    boundary. Fixed by requiring the same same-line evidence
    `build_section_map` requires (the tail of the line, after the
    number, must itself look like a caps title) before accepting a
    numbered line with MORE than one token as a header; a truly bare
    lone-number line (nothing else on it -- the FDA "number on its
    own line, title below" case) is still accepted outright, since
    that shape essentially never occurs mid-sentence in body prose or
    a dosage line (both always carry a unit/word alongside the
    number).
    """
    tokens = line_text.split()
    if not tokens:
        return False
    if line_text.strip().endswith(")") and "(" not in line_text:
        return False
    first = tokens[0]

    fused_match = _FUSED_NUMBER_HEADER_RE.match(first)
    if fused_match:
        num_part = fused_match.group(1)
        rest_part = fused_match.group(2)
        if rest_part.lower() not in _DOSE_UNITS:
            if "." in num_part:
                if rest_part[:1].isupper():
                    return True
            else:
                if _is_caps_line(" ".join([rest_part] + tokens[1:])):
                    return True

    if _NUMBERED_HEADER_RE.match(first):
        if len(tokens) == 1:
            return True
        return _is_caps_line(" ".join(tokens[1:]))
    if _SUBSECTION_HEADER_RE.match(first) and len(tokens) >= 2 and tokens[1][:1].isupper():
        return True
    if _SPEC_HEADER_RE.match(line_text) and _is_caps_line(line_text):
        return True
    return _looks_like_unnumbered_header(line_text)


# --- Column projection (extract_reading_order) --------------------------
# Two blocks are side-by-side columns only when their x-intervals are
# separated by a real gutter AND their y-ranges overlap (a same-column
# pair is stacked vertically, so its y-ranges never overlap). This is
# the same gutter test the pdfplumber word path uses, applied to blocks.
MIN_COLUMN_GUTTER_WIDTH = 12.0
_GUTTER_MERGE_PAD = 2.0


def _is_caps_or_unit_token(t: str) -> bool:
    """True if token is all-caps-ish, a number/punctuation, or a standard dose/strength unit."""
    clean = re.sub(r"[&'()/,.:\-%\ufeff]", "", t)
    if not clean:
        return True
    if clean.isdigit():
        return True
    if clean.isupper():
        return True
    parts = t.split("/")
    has_unit = False
    for p in parts:
        unit_clean = re.sub(r"[&'()/,.:\-%\d]", "", p).lower()
        if unit_clean in {"mg", "ml", "mlo", "mcg", "ug", "g", "kg", "mmol", "iu", "ppm", "l"}:
            has_unit = True
        elif unit_clean:
            return False
    return has_unit


def _is_substantive_caps_word(t: str) -> bool:
    """True if token contains at least one alphabetic character that is uppercase and not merely a dose unit."""
    clean = re.sub(r"[&'()/,.:\-%\ufeff\d]", "", t)
    if not clean:
        return False
    if clean.lower() in {"mg", "ml", "mlo", "mcg", "ug", "g", "kg", "mmol", "iu", "ppm", "l"}:
        return False
    return clean.isupper()


def _is_caps_line(text: str) -> bool:
    """True if every whitespace token is all-caps-ish (digits/punct/dose-units
    allowed) and at least one substantive uppercase word is present --
    "INDICATIONS AND USAGE (5.2)" yes, "1. QUÉ ES PRODUCTO..." yes,
    "Do not exceed" no, "250 mg / 5 mL" no."""
    tokens = text.split()
    return (
        bool(tokens)
        and all(_is_caps_or_unit_token(t) for t in tokens)
        and any(_is_substantive_caps_word(t) for t in tokens)
    )


def extract_reading_order(pdf_path: str) -> list[dict]:
    """Extract text blocks from a PDF in visual column-major order.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        A flat list of block dicts covering all pages in reading order:

            {"page": int,            # 1-indexed page number
             "bbox": [x0, y0, x1, y1],  # PDF points, y grows downward
             "text": str}            # the block's text (may span lines)

    Column projection algorithm
    ----------------------------
    1. Fetch every text block on the page via ``page.get_text("blocks")``
       (PyMuPDF returns ``(x0, y0, x1, y1, text, block_no, block_type)``
       tuples; we keep only text blocks with non-empty text).
    2. Sort blocks by left edge ``x0``, then sweep left-to-right and
       find GUTTERS: a gap between two consecutive blocks whose x-ranges
       do not touch AND whose y-ranges overlap. The y-overlap is the key
       signal -- blocks stacked vertically in one column share the x
       range but never overlap in y, while blocks in adjacent columns
       overlap in y but are separated in x. A page title that spans the
       full width touches both columns' blocks in x, so it never opens a
       gutter.
    3. Merge gutter sightings that sit at the same x position into one
       vertical gutter band; the regions between bands are the COLUMNS,
       left to right.
    4. Assign each block to the column whose band covers the majority of
       its x-interval (ties go to the leftmost band), so a full-width
       banner lands at the top of the reading order instead of the
       column its center happens to fall in.
    5. Emit columns left-to-right; within a column, blocks top-to-bottom
       (then left-to-right by ``x0``). Pages are emitted in order.
    """
    blocks_out: list[dict] = []
    with fitz.open(pdf_path) as doc:
        for page_num, page in enumerate(doc, start=1):
            raw_blocks = []
            for block in page.get_text("blocks"):
                # PyMuPDF: (x0, y0, x1, y1, text, block_no, block_type).
                # Older releases return 6-tuples; guard the type index.
                block_type = block[6] if len(block) > 6 else 0
                text = block[4].strip()
                if block_type == 0 and text:
                    raw_blocks.append(block)
            if not raw_blocks:
                continue
            for column in _project_columns(raw_blocks, page.rect.width, page.rect.height):
                for block in column:
                    blocks_out.append(
                        {
                            "page": page_num,
                            "bbox": [block[0], block[1], block[2], block[3]],
                            "text": block[4].strip(),
                        }
                    )
    return blocks_out


def _project_columns(
    blocks: list[tuple[float, float, float, float, str, int, int]],
    page_width: float,
    page_height: float,
) -> list[list[tuple[float, float, float, float, str, int, int]]]:
    """Group one page's text blocks into left-to-right column lists.

    See `extract_reading_order` for the algorithm. Returns a list of
    columns, each column a list of blocks already sorted top-to-bottom.
    """
    if not blocks:
        return []
    ordered = sorted(blocks, key=lambda b: (b[0], b[1]))

    # Step 2: candidate gutters from x-adjacent blocks whose y-ranges
    # overlap (the "side-by-side" signal).
    gutter_sightings: list[tuple[float, float]] = []
    for prev, nxt in zip(ordered, ordered[1:]):
        y_overlap = min(prev[3], nxt[3]) - max(prev[1], nxt[1])
        if nxt[0] - prev[2] >= MIN_COLUMN_GUTTER_WIDTH and y_overlap > 0:
            gutter_sightings.append((prev[2], nxt[0]))

    # Step 3: merge sightings at the same x position into gutter bands.
    gutters = _merge_gutter_gaps(gutter_sightings)
    if not gutters:
        # No persistent interior gutter: an ordinary single-column page
        # (or every block spans the full width). Simple y-order.
        return [sorted(ordered, key=lambda b: (b[1], b[0]))]

    # Step 4: column bands are the regions between gutters.
    bands: list[tuple[float, float]] = []
    prev_x = 0.0
    for gx0, gx1 in gutters:
        bands.append((prev_x, gx0))
        prev_x = gx1
    bands.append((prev_x, page_width))

    columns: list[list[tuple[float, float, float, float, str, int, int]]] = [
        [] for _ in bands
    ]
    for block in ordered:
        best_band = 0
        best_overlap = -1.0
        for band_idx, (lo, hi) in enumerate(bands):
            overlap = min(block[2], hi) - max(block[0], lo)
            if overlap > best_overlap:
                best_overlap = overlap
                best_band = band_idx
        columns[best_band].append(block)

    # Step 5: top-to-bottom within each column; drop empty bands.
    result = [
        sorted(col, key=lambda b: (b[1], b[0]))
        for col in columns
        if col
    ]
    return result


def _merge_gutter_gaps(gaps: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge (x0, x1) gutter sightings that touch or overlap into bands."""
    if not gaps:
        return []
    merged: list[list[float]] = []
    for lo, hi in sorted(gaps):
        if merged and lo <= merged[-1][1] + _GUTTER_MERGE_PAD:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]


# Ordinary sentence-initial capitalized words that must NEVER be
# absorbed into a subsection title, even though they pass the bare
# "starts with an uppercase letter" test the title-accumulation loop
# otherwise uses. Without this guard, a subsection title immediately
# followed by its own body sentence on the same physical line (a
# narrow-column layout artifact -- "2.4 Administration The effect of
# zolpidem...") greedily swallows the body's first word ("The") into
# the title, corrupting the section key used for cross-document
# matching. This list is deliberately small and only contains common
# function/pronoun words that are vanishingly unlikely to be a
# genuine trailing title word in an FDA-style numbered header.
_TITLE_STOP_WORDS = {
    "the", "this", "these", "those", "a", "an", "it", "if", "when",
    "because", "some", "such", "there", "due", "as", "may", "should",
    "will", "can", "does", "do", "did", "patients", "elderly",
}


def _peel_leading_caps(text: str) -> tuple[str, str]:
    """Split a line into (leading caps-token run, remainder).

    Used when a wrapped top-level header's last title word(s) sit on
    the same physical line as the section's body text with no line
    break between them ("2 DOSAGE AND" / "ADMINISTRATION The dose of
    ..." rendered as one line in a narrow-column layout). Peeling at
    the TOKEN level -- rather than requiring the whole next line to be
    all-caps -- recovers "ADMINISTRATION" as part of the title instead
    of losing it to whichever section's body happens to absorb it.

    Returns:
        (caps_prefix, remainder) -- caps_prefix is "" if the line does
        not start with at least one caps token (nothing to peel).
        remainder is the rest of the line, re-joined with single
        spaces (safe: remainder only ever feeds into body text, which
        is whitespace-normalized before comparison anyway).
    """
    tokens = text.split()
    prefix_tokens: list[str] = []
    for token in tokens:
        if _is_caps_or_unit_token(token) and any(ch.isalpha() for ch in token):
            prefix_tokens.append(token)
        else:
            break
    if not prefix_tokens:
        return "", text
    remainder = " ".join(tokens[len(prefix_tokens):])
    return " ".join(prefix_tokens), remainder


def build_section_map(blocks: list[dict]) -> dict[str, list[dict]]:
    """`build_section_list`, keyed by header text: chunks that share a
    header (a table-of-contents line and its section, a repeated
    "Manufactured by:") are merged under one key, in stream order."""
    sections: dict[str, list[dict]] = {}
    for key, section_blocks in build_section_list(blocks):
        sections.setdefault(key, []).extend(section_blocks)
    return sections


def build_section_list(blocks: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group blocks into semantic sections, one entry per detected header.

    Args:
        blocks: Block dicts in reading order (as returned by
            `extract_reading_order`, or any list with the same
            {"page", "bbox", "text"} schema; a block whose dict carries
            a truthy "_in_table" key -- e.g. a table-cell line tagged by
            the word pipeline -- can never be a header).

    Returns:
        (header, blocks) pairs in stream order: each detected section
        header (e.g. "2 DOSAGE AND ADMINISTRATION") with the blocks
        belonging to that section (header line(s) included). Blocks
        before the first header come first under the empty header ""
        (the preamble). A header that occurs twice gives two separate
        entries.

    Header detection
    ----------------
    A line starts a new section when any of these holds:

    * the spec regex matches AND the whole line is all-caps: a number
      (optionally "2.1"-style) followed by an all-caps title --
      "1 INDICATIONS AND USAGE", "2.1 DOSAGE IN ADULTS";
    * the line's first token is a bare number ("1", "1)", "1.") --
      the title is then the all-caps words on the same line or on the
      following lines, because narrow layouts wrap long titles across
      lines ("1 INDICATIONS AND" / "USAGE", "4 CONTRAINDICATION" / "S")
      and the whole wrapped run is ONE logical header;
    * the line starts with a N-dot-N token followed by a capitalized
      word -- a numbered SUBSECTION ("2.1 Dosage in adults"), which is
      its own chunk so column-interleaved overflow stays matchable.
    * the whole line is a short (<= 6 word) all-caps run of PURE-LETTER
      tokens (no digits, no embedded dots) -- an UNNUMBERED header
      (MVP-19), for documents that don't follow FDA/EMA-style
      numbering: "CARDMEMBER AGREEMENT", "INTEREST CHARGES",
      "ARBITRATION". The letters-only restriction is what keeps this
      from ever matching a document control code like
      "CM.TL24K.LIN.0215", which is nominally all-caps too but mixes
      digits and bare dots into its tokens the way no real title does.

    All-caps lines that follow a numbered line are consumed into its
    title; body lines (starting lowercase, or inside a table) never
    start a section.
    """
    # Split each block into its visual lines first: PyMuPDF blocks are
    # paragraphs and may span several lines, so a wrapped header must be
    # detected line-by-line even when a producer merged the paragraph.
    stream: list[tuple[str, dict]] = []
    for block in blocks:
        for line in block["text"].splitlines():
            stream.append((line, block))

    sections: list[tuple[str, list[dict]]] = []
    current: list[dict] | None = None  # blocks of the section being filled

    has_numbered = any(
        (_NUMBERED_HEADER_RE.match(line.split()[0]) and (len(line.split()) == 1 or _is_caps_line(" ".join(line.split()[1:]))))
        or (_SUBSECTION_HEADER_RE.match(line.split()[0]) and len(line.split()) >= 2 and line.split()[1][:1].isupper())
        for block in blocks for line in block.get("text", "").splitlines() if line.split() and not block.get("_in_table")
    )

    i = 0
    n = len(stream)
    while i < n:
        line_text, block = stream[i]
        tokens = line_text.split()

        header_title: Optional[str] = None
        consume_through = i  # last stream index that belongs to the header

        if tokens and not block.get("_in_table"):
            first = tokens[0]
            fused_match = _FUSED_NUMBER_HEADER_RE.match(first)
            if fused_match:
                num_p = fused_match.group(1)
                rest_p = fused_match.group(2)
                if rest_p.lower() not in _DOSE_UNITS:
                    if "." in num_p and rest_p[:1].isupper():
                        tokens = [num_p, rest_p] + tokens[1:]
                        first = num_p
                        line_text = num_p + " " + rest_p + (" " + " ".join(tokens[2:]) if len(tokens) > 2 else "")
                    elif "." not in num_p and _is_caps_line(" ".join([rest_p] + tokens[1:])):
                        tokens = [num_p, rest_p] + tokens[1:]
                        first = num_p
                        line_text = num_p + " " + rest_p + (" " + " ".join(tokens[2:]) if len(tokens) > 2 else "")

            if _NUMBERED_HEADER_RE.match(first):
                # Numbered line: the title is the words after the number
                # plus every following all-caps line (wrapped titles).
                # If the title's tail instead sits on the SAME line as
                # body text (no line break before the body starts --
                # narrow-column layouts do this), peel just the leading
                # caps tokens off that one line instead of requiring the
                # whole line to be caps (see `_peel_leading_caps`).
                #
                # Known limitation, found and deliberately left in place:
                # a bare page number immediately followed, on the SAME
                # line, by a print proof's date/time-stamp footer
                # ("1 12/4/13 2:39 PM") satisfies the same-line
                # `has_caps_words` check below (digits/slashes/colons
                # are valid caps-line punctuation, since a real title
                # legitimately uses them too: "FD&C BLUE NO. 2"), so it
                # gets accepted as a header and can absorb whatever REAL
                # header follows it into one garbled title. An earlier
                # version of this fix rejected any bare-number line
                # followed by a date/time token outright -- verified, on
                # the real document that surfaced this, to cause FAR
                # MORE damage than it fixed: the footer's tokens simply
                # became stray body content instead, corrupting word-
                # level alignment throughout the surrounding section.
                # One contained, ugly title beats that.
                title_parts = [line_text]
                has_caps_words = len(tokens) >= 2 and _is_caps_line(
                    " ".join(tokens[1:])
                )
                is_bare_number = len(tokens) == 1
                if has_caps_words or is_bare_number:
                    j = i + 1
                    while j < n:
                        nxt_text, nxt_block = stream[j]
                        if nxt_block.get("_in_table"):
                            break
                        if nxt_block.get("page") != block.get("page"):
                            break
                        if "bbox" in nxt_block and "bbox" in block:
                            if (nxt_block["bbox"][1] - block["bbox"][3]) > 60.0:
                                break
                        nxt_tokens = nxt_text.split()
                        if nxt_tokens and _NUMBERED_HEADER_RE.match(nxt_tokens[0]):
                            break
                        if _is_caps_continuation_line(nxt_text):
                            title_parts.append(nxt_text)
                            j += 1
                            continue
                        last_word = title_parts[-1].split()[-1].upper() if title_parts[-1].split() else ""
                        connectors = {
                            "AND", "OR", "FOR", "OF", "IN", "WITH", "TO", "THE", "A", "AN", "&", "/", "-",
                            "Y", "E", "O", "U", "DE", "DEL", "PARA", "POR", "CON", "EN", "SE", "QUE"
                        }
                        if is_bare_number or last_word in connectors or last_word.endswith("-"):
                            caps_prefix, remainder = _peel_leading_caps(nxt_text)
                            if caps_prefix and caps_prefix.lower() not in _DOSE_UNITS:
                                title_parts.append(caps_prefix)
                                if remainder:
                                    stream[j] = (remainder, nxt_block)
                                else:
                                    j += 1
                        break
                    if has_caps_words or (is_bare_number and j > i + 1):
                        header_title = " ".join(title_parts)
                        consume_through = j - 1
            elif _SUBSECTION_HEADER_RE.match(first) and len(tokens) >= 2 and tokens[1][:1].isupper():
                # "2.1 Dosage in adults": title keeps the leading
                # capitalized words plus connector words ("in", "of"...)
                # and stops at the first lowercase body word -- OR at a
                # sentence-initial stopword ("The", "Because", ...),
                # which guards against absorbing a body sentence's first
                # word when the title and body share one physical line
                # (see `_TITLE_STOP_WORDS`).
                title_tokens = [first]
                for token in tokens[1:]:
                    if token.casefold() in _TITLE_STOP_WORDS:
                        break
                    if token[:1].isupper() or token in _SUBSECTION_CONNECTORS:
                        title_tokens.append(token)
                    else:
                        break
                header_title = " ".join(title_tokens)
            elif _SPEC_HEADER_RE.match(line_text) and _is_caps_line(line_text):
                # The spec regex on its own: a number (any length) with
                # an all-caps title, possibly wrapped onto following
                # all-caps lines ("17 WARNINGS AND PRECAUTIONS"), or onto
                # a leading caps run sharing a line with body text (see
                # the numbered-header branch above for why).
                title_parts = [line_text]
                j = i + 1
                while j < n:
                    nxt_text, nxt_block = stream[j]
                    if nxt_block.get("_in_table"):
                        break
                    if nxt_block.get("page") != block.get("page"):
                        break
                    if "bbox" in nxt_block and "bbox" in block:
                        if (nxt_block["bbox"][1] - block["bbox"][3]) > 60.0:
                            break
                    if _is_caps_continuation_line(nxt_text):
                        title_parts.append(nxt_text)
                        j += 1
                        continue
                    last_word = title_parts[-1].split()[-1].upper() if title_parts[-1].split() else ""
                    connectors = {
                        "AND", "OR", "FOR", "OF", "IN", "WITH", "TO", "THE", "A", "AN", "&", "/", "-",
                        "Y", "E", "O", "U", "DE", "DEL", "PARA", "POR", "CON", "EN", "SE", "QUE"
                    }
                    if last_word in connectors or last_word.endswith("-"):
                        caps_prefix, remainder = _peel_leading_caps(nxt_text)
                        if caps_prefix:
                            title_parts.append(caps_prefix)
                            if remainder:
                                stream[j] = (remainder, nxt_block)
                            else:
                                j += 1
                    break
                header_title = " ".join(title_parts)
                consume_through = j - 1
            elif _looks_like_unnumbered_header(line_text) and not (has_numbered and line_text.strip().endswith(":")) and not (
                # Reject when either neighbor ALSO has the header shape
                # -- a genuine header sits between ordinary body text
                # (or the document's very start/end) on both sides; two
                # caption-shaped lines touching each other are a
                # multi-line document TITLE, not two separate sections
                # (see _looks_like_unnumbered_header's docstring for the
                # real "FULL PRESCRIBING" / "INFORMATION" case this
                # guards against).
                (i > 0 and _looks_like_unnumbered_header(stream[i - 1][0]))
                or (i + 1 < n and _looks_like_unnumbered_header(stream[i + 1][0]))
                or (i > 0 and stream[i - 1][0].strip().split() and stream[i - 1][0].strip().split()[-1].rstrip(",;:.-").lower() in {
                    "and", "or", "for", "of", "in", "with", "to", "the", "a", "an", "&", "/", "-",
                    "y", "e", "o", "u", "de", "del", "para", "por", "con", "en", "se", "que"
                } and (
                    stream[i - 1][1].get("page") == block.get("page")
                    and "bbox" in stream[i - 1][1] and "bbox" in block
                    and abs(stream[i - 1][1]["bbox"][1] - block["bbox"][1]) <= 60.0
                ))
            ):
                # MVP-19: an UNNUMBERED all-caps header (see
                # _UNNUMBERED_HEADER_WORD_RE above) -- documents that
                # don't follow FDA/EMA-style numbering (credit-card
                # agreements, terms of service) still have real,
                # addressable sections ("CARDMEMBER AGREEMENT",
                # "INTEREST CHARGES", "ARBITRATION"), just without a
                # leading number to anchor on. No multi-line wrap
                # support here (unlike the numbered branches above) --
                # every real case observed so far is a single physical
                # line, and guessing where an unnumbered caps run ends
                # is a materially riskier heuristic than the numbered
                # case, where the leading digit is a strong anchor.
                header_title = line_text

        if header_title is not None:
            current = []
            sections.append((header_title, current))
            for k in range(i, consume_through + 1):
                current.append(stream[k][1])
            i = consume_through + 1
        else:
            if current is None:
                current = []
                sections.append(("", current))
            current.append(block)
            i += 1

    return sections


def section_key(title: str) -> tuple[Optional[str], str]:
    """Normalized (section number, title letters) key for a header.

    The number is the leading integer ("2") or subsection ("2.1") kept
    as a string; the title is the remaining words with everything but
    letters stripped and lowercased, so "4 CONTRAINDICATION S" and
    "4 CONTRAINDICATIONS" normalize to the same key even though a
    narrow layout split the title across lines mid-word. A preamble
    (no header) yields (None, "").
    """
    if not title:
        return (None, "")
    parts = title.split()
    number: Optional[str] = None
    first_clean = parts[0].rstrip(".)")
    if re.match(r"^\d{1,3}(\.\d{1,3})?$", first_clean):
        number = first_clean
        parts = parts[1:]
    letters = "".join(ch for ch in " ".join(parts) if ch.isalpha()).casefold()
    return (number, letters)


def _normalize_section_key(key: str) -> str:
    """Case/whitespace/punctuation-insensitive form of a header key.

    Used only to pair keys that differ by a wrap artifact ("4
    CONTRAINDICATION S" vs "4 CONTRAINDICATIONS") when an exact-key
    match is unavailable.
    """
    return "".join(ch for ch in key if ch.isalnum()).casefold()


def _blocks_text(blocks: list[dict]) -> str:
    """Join a section's block texts into one comparison string."""
    return " ".join(block["text"].strip() for block in blocks)


_DMP = diff_match_patch()


def diff_section_maps(
    app_map: dict[str, list[dict]],
    susp_map: dict[str, list[dict]],
) -> list[dict]:
    """Diff two section maps strictly by section key.

    Args:
        app_map: `build_section_map` output for the approved document.
        susp_map: `build_section_map` output for the suspect document.

    Returns:
        Result dicts, in approved section order (then suspect-only
        sections), one per changed hunk or missing section:

            {"kind": "text_changed",          # words differ inside a
             "section": "<key>",              #  matched section
             "approved_text": str, "suspect_text": str,
             "approved_blocks": [...], "suspect_blocks": [...]}

            {"kind": "removed_in_suspect", ...}   # hunk with only
            {"kind": "added_in_suspect", ...}     # approved/suspect words

            {"kind": "deleted_section",       # key present only in app
             "section": "<key>",
             "approved_blocks": [...], "suspect_blocks": []}

            {"kind": "inserted_section", ...} # key present only in susp

    Matching is BY KEY: a section is compared only with the section
    carrying the same header on the other side (exact key first, then a
    case/whitespace-insensitive fallback for wrap artifacts), never with
    an unrelated section -- so a section moved to the front of a leaflet
    is matched and diffed in place, and produces no cascade. Missing
    keys become a single deleted/inserted entry WITHOUT any text diff.
    Text diffs run on matched sections via Google diff-match-patch at
    WORD granularity (words are char-encoded so the matcher stays fast
    even on long sections); identical sections produce no entries.

    Note: the header line is part of a section's blocks, so a header
    split across lines by a narrow layout ("4 CONTRAINDICATION S" vs
    "4 CONTRAINDICATIONS") can surface as a low-severity text_changed
    on the header itself -- the word engine classifies the same artifact
    as a typo. The KEYED pairing (no deleted/inserted, no cascade) is
    unaffected.
    """
    results: list[dict] = []

    matched_app: set[str] = set()
    matched_susp: set[str] = set()

    # 1. Exact-key matches.
    for key in app_map:
        if key in susp_map:
            results.extend(_diff_matched_section(key, app_map[key], susp_map[key]))
            matched_app.add(key)
            matched_susp.add(key)

    # 2. Wrap-artifact fallback: keys on one side whose normalized form
    #    matches a key on the other ("4 CONTRAINDICATION S" vs
    #    "4 CONTRAINDICATIONS"). First occurrence wins.
    norm_susp: dict[str, str] = {}
    for key in susp_map:
        norm_susp.setdefault(_normalize_section_key(key), key)
    for key in app_map:
        if key in matched_app:
            continue
        partner = norm_susp.get(_normalize_section_key(key))
        if partner is not None and partner not in matched_susp:
            results.extend(_diff_matched_section(key, app_map[key], susp_map[partner]))
            matched_app.add(key)
            matched_susp.add(partner)

    # 3. Sections present on only one side: one deleted/inserted entry,
    #    never a text diff against an unrelated section.
    for key in app_map:
        if key not in matched_app:
            results.append(
                {
                    "kind": "deleted_section",
                    "section": key,
                    "approved_blocks": app_map[key],
                    "suspect_blocks": [],
                }
            )
    for key in susp_map:
        if key not in matched_susp:
            results.append(
                {
                    "kind": "inserted_section",
                    "section": key,
                    "approved_blocks": [],
                    "suspect_blocks": susp_map[key],
                }
            )
    return results


def _words_to_chars(text1: str, text2: str) -> tuple[str, str, list[str]]:
    """Encode two texts as single-char runs, one char per unique word.

    A "word" is a maximal run of alphanumeric characters; every other
    character (space, punctuation) is kept as its own token so the
    decoded text reproduces the input exactly. The returned word_array
    maps ``chr(ord + 1)`` back to each token. This is the classic
    diff-match-patch word-encoding trick (the Python port only ships
    the line-level ``diff_linesToChars``): `diff_main` then runs on
    short single-char strings, which keeps it fast even for long
    sections.
    """
    token_list: list[str] = []
    token_hash: dict[str, int] = {}

    def encode(text: str) -> str:
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            j = i
            while j < n and text[j].isalnum():
                j += 1
            if j == i:  # a separator character is its own token
                token = text[i]
                j = i + 1
            else:
                token = text[i:j]
            idx = token_hash.get(token)
            if idx is None:
                idx = len(token_list)
                token_list.append(token)
                token_hash[token] = idx
            out.append(chr(idx + 1))  # 1-based; chr(0) is reserved
            i = j
        return "".join(out)

    return encode(text1), encode(text2), token_list


def _diff_matched_section(
    key: str,
    app_blocks: list[dict],
    susp_blocks: list[dict],
) -> list[dict]:
    """diff-match-patch word diff of one matched section pair.

    Words are char-encoded (`_words_to_chars`) so `diff_main` runs on
    short char strings (fast even for long sections) and the decoded
    result is a list of word ops (-1 = only in approved, 1 = only in
    suspect, 0 = equal). Consecutive non-equal ops are grouped into
    hunks -- one result dict per hunk.
    """
    chars1, chars2, word_array = _words_to_chars(
        _blocks_text(app_blocks), _blocks_text(susp_blocks)
    )
    diffs = _DMP.diff_main(chars1, chars2, checklines=False)
    _DMP.diff_cleanupSemantic(diffs)
    word_ops: list[tuple[int, str]] = []
    for op, chars in diffs:
        word_ops.append((op, "".join(word_array[ord(c) - 1] for c in chars)))

    entries: list[dict] = []
    approved: list[str] = []
    suspect: list[str] = []
    for op, token in word_ops:
        if op == 0:
            if approved or suspect:
                entries.append(
                    _hunk_entry(key, approved, suspect, app_blocks, susp_blocks)
                )
                approved, suspect = [], []
        elif op == -1:
            approved.append(token)
        else:  # op == 1
            suspect.append(token)
    if approved or suspect:
        entries.append(_hunk_entry(key, approved, suspect, app_blocks, susp_blocks))
    return entries


def _hunk_entry(
    key: str,
    approved: list[str],
    suspect: list[str],
    app_blocks: list[dict],
    susp_blocks: list[dict],
) -> dict:
    """One result dict from a group of consecutive non-equal word ops."""
    if approved and suspect:
        kind = "text_changed"
    elif approved:
        kind = "removed_in_suspect"
    else:
        kind = "added_in_suspect"
    return {
        "kind": kind,
        "section": key,
        "approved_text": "".join(approved),
        "suspect_text": "".join(suspect),
        "approved_blocks": app_blocks,
        "suspect_blocks": susp_blocks,
    }
