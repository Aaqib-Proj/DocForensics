"""Word-stream comparison with coordinates (core/diff_engine.py).

Milestone scope: comparison ONLY, building on core/pdf_extractor.py.
No Qt imports -- standalone-testable.

--------------------------------------------------------------------
Why this doesn't need manual O(N) "chunking"
--------------------------------------------------------------------
difflib.SequenceMatcher is already near-linear in practice (it uses
the Ratcliff/Obershelp longest-matching-block algorithm with a
junk-element heuristic). The earlier block-string approach was slow
and noisy because it fed whole multi-line block strings as the
sequence *elements* being matched char-by-char inside HtmlDiff. Here
the sequence elements are individual WORDS (typically a few hundred
to a few thousand per document), so the matcher's real work is on a
short sequence, not raw characters -- that's what keeps this fast,
not a hand-rolled chunking loop.

--------------------------------------------------------------------
MVP-5: severity ruleset (fixes the MVP-4 "cheap first pass")
--------------------------------------------------------------------
Real source PDFs -- even the "approved" reference copy -- routinely
carry small, pre-existing text-layer typos that have nothing to do
with tampering: a dropped double letter plus a stray space
("especially" -> "especial y", "illness" -> "il ness"), or a single
missing letter ("hallucinations" -> "halucinations"). These are
character-level noise, not meaning changes, and on a document with a
few dozen of them they will bury the one discrepancy that actually
matters (e.g. a "5 mg" that silently became "10 mg") if every
non-identical word is reported at the same severity.

The ruleset below is a strict PRIORITY LADDER, most dangerous first:

1. Any word present on one side and missing on the other (insert/
   delete) -- always "critical". There's no safe way to auto-downgrade
   a disappearing/appearing word, so this is unconditional.
2. A "replace" where the DIGIT CONTENT differs between the two sides
   (e.g. "5 mg" -> "10 mg") -- always "critical". This catches dosage/
   strength/quantity tampering. The check compares the ordered digit
   runs, so the same digits surrounded by different non-digit context
   (e.g. RTL bracket mirroring) are NOT escalated -- this avoids the
   false amplification that occurred with Arabic pharmacological text
   where essentially every token contains incidental digits.
3. Case-only change (e.g. "ROPINIROLE" vs "ropinirole") -- "major".
4. Punctuation-only change -- "minor".
5. Typo-like change: both sides are pure alphabetic text, close in
   length, and highly similar once whitespace is stripped (catches
   the "especial y" / "il ness" / "halucinations" pattern, where a
   dropped letter or an inserted mid-word space is the entire
   difference) -- "typo". This is the lowest severity tier, purely so
   these sort/filter to the bottom -- they are never hidden or
   dropped from the results.
6. Anything else -- a genuine wording change -- "critical".

Severity order for sorting/filtering purposes:
    critical > major > minor > typo > reorder

The lowest tier, "reorder" (MVP-7), is produced by the pairing pass
in `_downgrade_reorder_pairs`: identical text flagged removed on one
side and added on the other is a reading-order artifact (a word that
wrapped to an adjacent line, a column read in a marginally different
order), not a content change. Those pairs are relabeled "reordered"
and sorted to the bottom -- visible, never dropped.

--------------------------------------------------------------------
MVP-8: out-of-order section matching
--------------------------------------------------------------------
Real production adaptation -- full manuals adapted into pocket
guides, folding-carton inserts, or regional leaflets -- routinely
REORDERS whole sections (Warnings & Precautions pulled to the front
for a concise leaflet, sections relocated to fit panel boundaries,
FDA vs EMA section hierarchies). A linear word-stream diff then
reports the moved section as a critical "removed + added" pair (or
worse, a cascade if the moved text partially matches its neighbors).

`diff_documents` therefore does a section-aware pre-pass when BOTH
documents contain at least two numbered section headers (e.g.
"1 INDICATIONS AND USAGE"):

1. HEADER-BASED CHUNKING: `_split_sections` splits each token stream
   into sections at numbered headers -- TOP-LEVEL all-caps headers
   ("1 INDICATIONS AND USAGE", possibly wrapped across lines by a
   narrow layout: "1 INDICATIONS AND" + "USAGE", "4 CONTRAINDICATION"
   + "S") AND numbered SUBSECTIONS ("2.1 Dosage in adults", "5.3
   Abnormal Thinking"). Subsection chunking matters because leaflets
   interleave sections ACROSS columns, so a top-level chunk can absorb
   another section's overflow that has its own numbered subsection.
   The header detector itself lives in core/section_diff.py
   (`build_section_map`) -- the engine converts its token lines into
   block dicts and calls it, so there is exactly one header detector
   for both the block-level and the word-level pipelines.
2. GLOBAL BLOCK LOOKUP: `_match_sections` matches each suspect
   section against ALL approved sections, position-agnostic, with
   TITLE-ANCHORED matching first (same header number + similar title
   -> paired regardless of body content; subsections anchor on the
   number alone) and token-sequence similarity as the fallback for
   unnumbered/renumbered sections. Title anchoring keeps "2 DOSAGE"
   paired with "2 DOSAGE" even when a contaminated chunk made their
   bodies look alike.
3. LOCAL DIFFING: matched section pairs are diffed against each other
   in isolation (the same word-stream matcher, scoped to the
   section), so a moved section's genuine internal edits are still
   caught at their real severity -- while its identical words produce
   nothing. Sections whose MATCHED POSITION RANK changed are reported
   as one low-severity "reordered_section" item instead of critical
   removed/added noise; sections present on only one side become a
   single removed/added item (never a cascade).
4. RELOCATION DEMOTION: `_downgrade_relocated_runs` demotes any
   multi-word removed/added/replace run that exists VERBATIM
   elsewhere in the other document to "reorder" -- the signature of
   a section fragment printed in a different column (section 3's body
   in a side panel), present on both sides but not at the same
   position. Genuine single-word edits and runs that differ by even
   one word stay at their real severity.

5. MOVED-TEXT PASS: section chunking depends on header detection, and
   a print layout can defeat it -- e.g. a figure's axis label read as a
   heading swallows the rest of a section into an unrelated chunk.
   Whatever the section pairs leave unexplained (removed runs, added
   runs, both sides of replacements, unmatched sections) is searched
   for runs of `_MIN_MOVED_RUN_WORDS`+ identical words present on both
   sides. Those are paired as moved text (one low-severity "reordered"
   row per moved passage) and the remaining text is diffed again
   without them, so only genuine differences are reported.

Documents without detected numbered headers (plain paragraphs, a
single header, or tables) fall back to the original whole-stream
word diff -- byte-for-byte the MVP-1..7 behavior.
"""

from __future__ import annotations

import bisect
import difflib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from core.pdf_extractor import ComparisonToken, Word
from core.script_text import SCRIPT_DIGITS
from core.section_diff import build_section_list, section_key

# Map difflib opcodes -> a stable name for the UI/report layer.
_TAG_LABELS = {
    "replace": "text_changed",
    "delete": "removed_in_suspect",
    "insert": "added_in_suspect",
}

# Severities in the order they should be displayed/prioritized.
# "reorder" (MVP-7) is the lowest tier: identical text that appears on
# both sides but at different positions in the stream -- a reading-
# order artifact, not a content change. Sorted last, never dropped.
SEVERITY_ORDER = ("critical", "major", "minor", "typo", "reorder")

# A "replace" is only eligible for the typo downgrade when the two
# sides, with whitespace stripped, are at least this similar
# (difflib.SequenceMatcher ratio, 0-1). Tuned so a single dropped
# letter or inserted space in a 6+ letter word passes comfortably,
# while genuinely different words of similar length do not.
_TYPO_SIMILARITY_THRESHOLD = 0.82

# Typo-candidate sides also can't differ in stripped length by more
# than this many characters -- guards against a short word matching a
# much longer one just because ratio() is lenient on short strings.
_TYPO_MAX_LENGTH_DELTA = 2

# Words whose `top` coordinates land within this many PDF points of
# each other are treated as the same visual line when building per-
# line highlight boxes (baseline jitter within one line is smaller
# than the spacing between lines, so bucketing by round(top / tol)
# keeps one line together without merging adjacent lines).
_LINE_BUCKET_TOLERANCE = 3.0


@dataclass
class Location:
    """Where a discrepancy's text actually sits, possibly spanning
    MULTIPLE pages (e.g. a paragraph that starts at the bottom of page
    2 and continues onto page 3, or -- the case that motivated this --
    a multi-page document being compared against a 1-page "concise"
    version where the SAME text search still has to resolve to
    whichever page it's really on in each document).

    `page`/`bbox` remain as a convenience "primary" page+box (the
    first page this Location touches, and the union box on that page
    only) so existing single-page call sites keep working unchanged.
    `boxes_by_page` is the authoritative, complete data: every page
    this discrepancy's text touches, each with its own per-line boxes.
    A UI that only calls `highlight_pdf_bbox(loc.page, loc.boxes)`
    will still work but will silently miss content on any OTHER page
    the Location spans -- see `iter_pages()` for the correct way to
    walk every page a Location touches.
    """

    page: int
    bbox: tuple[float, float, float, float]  # (x0, top, x1, bottom) -- union on `page` only
    # One horizontal box per visual text line in PDF points, for
    # `page` only. Kept for existing single-page callers; prefer
    # `boxes_by_page` for anything that must be correct across a
    # page-spanning or cross-document-length discrepancy.
    boxes: list[tuple[float, float, float, float]] = field(default_factory=list)
    # {page_number: [per-line boxes on that page]} -- every page this
    # discrepancy's text actually touches. Always contains at least
    # `page`; may contain more.
    boxes_by_page: dict[int, list[tuple[float, float, float, float]]] = field(
        default_factory=dict
    )

    def iter_pages(self) -> list[tuple[int, list[tuple[float, float, float, float]]]]:
        """Every (page, boxes) pair this Location touches, in page
        order. The correct way to highlight/navigate a Location that
        might span more than one page -- iterate this instead of only
        using `.page`/`.boxes`."""
        if self.boxes_by_page:
            return sorted(self.boxes_by_page.items())
        return [(self.page, self.boxes)]


@dataclass
class DiffItem:
    """One flagged discrepancy, ready for a clickable UI row.

    `approved_location` / `suspect_location` are None for pure
    insertions/deletions (the text genuinely doesn't exist on that
    side, so there's nowhere to point to).

    `approved_caret` / `suspect_caret` are set ONLY when the
    corresponding `*_location` above is None: a small, roughly
    one-character-wide marker at the INSERTION POINT in that
    document's own coordinate space -- see `_caret_location`. This is
    what the UI should draw a "missing" marker at instead of reusing
    the OTHER document's (potentially much larger, and from an
    entirely different layout) bbox, which used to make a one-word
    insertion highlight as if a whole sentence were missing at an
    arbitrary spot. May still be None if no neighboring token with real
    coordinates exists on either side (e.g. an empty document).
    """

    index: int
    kind: str  # "text_changed" | "removed_in_suspect" | "added_in_suspect"
    severity: str  # "critical" | "major" | "minor" | "typo"
    approved_text: str
    suspect_text: str
    approved_location: Optional[Location]
    suspect_location: Optional[Location]
    approved_caret: Optional[Location] = None
    suspect_caret: Optional[Location] = None


# (approved token, suspect token) pairs the matcher found identical.
TokenMatches = list[tuple[ComparisonToken, ComparisonToken]]


def diff_documents(
    approved_tokens: list[ComparisonToken],
    suspect_tokens: list[ComparisonToken],
    matches: Optional[TokenMatches] = None,
) -> list[DiffItem]:
    """Diff two token streams and return only the discrepancies.

    Args:
        approved_tokens: Tokens from the reference/approved PDF.
        suspect_tokens: Tokens from the PDF being checked.

    Returns:
        DiffItems for every non-equal opcode, in document order.
        Equal spans (the vast majority of a reflowed-but-unchanged
        document) are dropped -- callers only need what's wrong.

    When BOTH documents contain at least two numbered section headers,
    the comparison runs per-section (see the MVP-8 module note): moved
    sections are matched globally and reported as "reordered_section"
    instead of removed/added cascades. Otherwise the whole-stream
    word diff runs unchanged.

    If ``matches`` is given, every pair of tokens found identical (and
    therefore corresponding to each other) is appended to it -- the UI
    uses this to take the other document to the same text.
    """
    sections_a = _split_sections(approved_tokens)
    sections_b = _split_sections(suspect_tokens)
    if sections_a is not None and sections_b is not None:
        return _diff_by_sections(
            approved_tokens, suspect_tokens, sections_a, sections_b, matches
        )
    return _finalize_items(
        _diff_runs(approved_tokens, suspect_tokens, matches), approved_tokens, suspect_tokens
    )


def _diff_runs(
    approved_tokens: list[ComparisonToken],
    suspect_tokens: list[ComparisonToken],
    matches: Optional[TokenMatches] = None,
) -> list[DiffItem]:
    """Match two token streams and return DiffItems for every
    non-equal span, using plain-text sequence matching
    (difflib.SequenceMatcher, the Ratcliff/Obershelp algorithm).

    This deliberately does NOT depend on any third-party matching
    engine: difflib is in the Python standard library, so this
    function's behavior can never silently change based on whether an
    optional package happens to be installed. A previous version of
    this function tried a "hierarchical bipartite" matcher
    (core/zero_defect_engine.py, since removed) wrapped in a bare
    ``except Exception: pass`` that silently fell back to this exact
    difflib call on failure -- which meant the app's actual diff
    behavior depended on whether networkx/scipy/rapidfuzz happened to
    be importable, undeclared in requirements.txt, with no visible
    difference to the user either way. Verified against real
    fragmentation cases (an inserted "THE" in the middle of "KEEP OUT
    OF [THE] REACH OF CHILDREN" was being reported as two unrelated
    add/remove findings instead of one contextual insertion) that the
    bipartite matcher produced wrong results here; plain difflib does
    not.
    """
    return _items_from_opcodes(
        approved_tokens, suspect_tokens, _opcodes(approved_tokens, suspect_tokens), matches
    )


def _opcodes(
    approved_tokens: list[ComparisonToken], suspect_tokens: list[ComparisonToken]
) -> list[tuple[str, int, int, int, int]]:
    # autojunk=False: with autojunk on, SequenceMatcher can silently
    # treat any word appearing in >1% of a long document (e.g. "the",
    # "and") as "junk" and refuse to match it -- exactly the common
    # words a proofing tool most needs to track correctly.
    matcher = difflib.SequenceMatcher(
        None,
        [t.text for t in approved_tokens],
        [t.text for t in suspect_tokens],
        autojunk=False,
    )
    return matcher.get_opcodes()


def _items_from_opcodes(
    approved_tokens: list[ComparisonToken],
    suspect_tokens: list[ComparisonToken],
    opcodes: list[tuple[str, int, int, int, int]],
    matches: Optional[TokenMatches] = None,
) -> list[DiffItem]:
    items: list[DiffItem] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            if matches is not None:
                matches.extend(zip(approved_tokens[i1:i2], suspect_tokens[j1:j2]))
            continue

        approved_slice = approved_tokens[i1:i2]
        suspect_slice = suspect_tokens[j1:j2]
        # A "replace" spanning several words on each side is usually
        # several SEPARATE small edits sitting next to each other
        # (e.g. one word changed, then two words later another word
        # changed) rather than one coherent phrase change. Reporting
        # the whole span as a single finding hides which word(s)
        # actually differ and inflates severity (any digit anywhere in
        # the span marks the WHOLE span critical). Re-diffing at
        # CHARACTER granularity within same-length-ish replace spans
        # recovers the individual word-level edits; spans of very
        # different lengths (a real phrase rewrite) are left intact,
        # since character-level realignment there tends to produce
        # meaningless partial-word fragments instead of cleaner
        # results.
        if tag == "replace" and len(approved_slice) > 1 and len(suspect_slice) > 1:
            items.extend(
                _diff_replace_span(
                    approved_slice, suspect_slice, base_index=len(items), matches=matches
                )
            )
            continue
        items.append(
            _make_diff_item(
                tag,
                approved_slice,
                suspect_slice,
                index=len(items),
                approved_tokens=approved_tokens,
                suspect_tokens=suspect_tokens,
                i1=i1,
                i2=i2,
                j1=j1,
                j2=j2,
            )
        )
    return items


def _make_diff_item(
    tag: str,
    approved_slice: list[ComparisonToken],
    suspect_slice: list[ComparisonToken],
    index: int,
    *,
    approved_tokens: list[ComparisonToken],
    suspect_tokens: list[ComparisonToken],
    i1: int,
    i2: int,
    j1: int,
    j2: int,
) -> DiffItem:
    severity = _classify_severity(tag, approved_slice, suspect_slice)
    approved_location = _location_of(approved_slice)
    suspect_location = _location_of(suspect_slice)
    return DiffItem(
        index=index,
        kind=_TAG_LABELS[tag],
        severity=severity,
        approved_text=" ".join(t.text for t in approved_slice),
        suspect_text=" ".join(t.text for t in suspect_slice),
        approved_location=approved_location,
        suspect_location=suspect_location,
        approved_caret=(
            _caret_location(approved_tokens, i1, i2) if approved_location is None else None
        ),
        suspect_caret=(
            _caret_location(suspect_tokens, j1, j2) if suspect_location is None else None
        ),
    )


def _normalize_token_text(text: str) -> str:
    """Strip non-alphanumeric characters and lowercase for fuzzy word alignment."""
    import re
    return re.sub(r"[^\w]", "", text).lower()


def _diff_replace_span(
    approved_slice: list[ComparisonToken],
    suspect_slice: list[ComparisonToken],
    base_index: int,
    matches: Optional[TokenMatches] = None,
) -> list[DiffItem]:
    """Re-diff a multi-word 'replace' span word-by-word, recovering
    individual word-level edits difflib's own SequenceMatcher already
    finds correctly at the word-stream level -- this just applies the
    SAME matcher one level deeper, purely to avoid reporting an entire
    multi-word span as one finding when only one word inside it
    actually differs."""
    approved_words = [t.text for t in approved_slice]
    suspect_words = [t.text for t in suspect_slice]
    matcher = difflib.SequenceMatcher(None, approved_words, suspect_words, autojunk=False)

    exact_matches = [b for b in matcher.get_matching_blocks() if b.size > 0]

    # If exact word match found NO common words but both slices have multiple words,
    # try normalized word matching (ignoring punctuation & case) to isolate individual edits
    # instead of lumping whole sentences/paragraphs into one massive diff.
    if not exact_matches and len(approved_words) > 1 and len(suspect_words) > 1:
        norm_a = [_normalize_token_text(w) for w in approved_words]
        norm_b = [_normalize_token_text(w) for w in suspect_words]
        m2 = difflib.SequenceMatcher(None, norm_a, norm_b, autojunk=False)
        norm_matches = [b for b in m2.get_matching_blocks() if b.size > 0]

        if norm_matches:
            items: list[DiffItem] = []
            for tag, i1, i2, j1, j2 in m2.get_opcodes():
                if tag == "equal":
                    sub_a = approved_slice[i1:i2]
                    sub_b = suspect_slice[j1:j2]
                    for k in range(len(sub_a)):
                        tok_a = sub_a[k]
                        tok_b = sub_b[k]
                        if tok_a.text == tok_b.text:
                            if matches is not None:
                                matches.append((tok_a, tok_b))
                        else:
                            items.append(
                                _make_diff_item(
                                    "replace",
                                    [tok_a],
                                    [tok_b],
                                    index=base_index + len(items),
                                    approved_tokens=approved_slice,
                                    suspect_tokens=suspect_slice,
                                    i1=i1 + k,
                                    i2=i1 + k + 1,
                                    j1=j1 + k,
                                    j2=j1 + k + 1,
                                )
                            )
                else:
                    items.append(
                        _make_diff_item(
                            tag,
                            approved_slice[i1:i2],
                            suspect_slice[j1:j2],
                            index=base_index + len(items),
                            approved_tokens=approved_slice,
                            suspect_tokens=suspect_slice,
                            i1=i1,
                            i2=i2,
                            j1=j1,
                            j2=j2,
                        )
                    )
            return items

    items: list[DiffItem] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            if matches is not None:
                matches.extend(zip(approved_slice[i1:i2], suspect_slice[j1:j2]))
            continue
        items.append(
            _make_diff_item(
                tag,
                approved_slice[i1:i2],
                suspect_slice[j1:j2],
                index=base_index + len(items),
                approved_tokens=approved_slice,
                suspect_tokens=suspect_slice,
                i1=i1,
                i2=i2,
                j1=j1,
                j2=j2,
            )
        )
    return items


def _finalize_items(
    items: list[DiffItem],
    approved_tokens: Optional[list[ComparisonToken]] = None,
    suspect_tokens: Optional[list[ComparisonToken]] = None,
) -> list[DiffItem]:
    """Shared tail of every diff path: filter identical-text whitespace
    artifacts, reorder-pair demotion, severity sort, and stable index
    renumbering."""
    items = [
        item
        for item in items
        if not (
            item.kind == "text_changed"
            and re.sub(r"\s+", "", item.approved_text) == re.sub(r"\s+", "", item.suspect_text)
        )
    ]
    items = _downgrade_reorder_pairs(items)
    if approved_tokens is not None and suspect_tokens is not None:
        _downgrade_relocated_runs(items, approved_tokens, suspect_tokens)
        items = _deduplicate_reordered_pairs(items)
    items.sort(key=lambda item: SEVERITY_ORDER.index(item.severity))
    for new_index, item in enumerate(items):
        item.index = new_index
    return items


def _downgrade_reorder_pairs(items: list[DiffItem]) -> list[DiffItem]:
    """Pair identical-text removals/additions and demote to 'reordered'.
    Keeps only the Original reference, setting its suspect_location to the
    paired addition's location and discarding the duplicate addition, so
    reordered text is reported only ONCE rather than twice.
    """
    added_by_text: dict[str, list[DiffItem]] = {}
    for i in items:
        if i.kind == "added_in_suspect":
            added_by_text.setdefault(i.suspect_text, []).append(i)

    items_to_drop: set[int] = set()

    for item in items:
        if item.kind == "removed_in_suspect":
            text = item.approved_text
            matching_added = added_by_text.get(text)
            if matching_added:
                added_item = matching_added.pop(0)
                item.kind = "reordered"
                item.severity = "reorder"
                item.suspect_text = added_item.suspect_text
                item.suspect_location = added_item.suspect_location
                item.suspect_caret = added_item.suspect_caret
                items_to_drop.add(id(added_item))

    return [i for i in items if id(i) not in items_to_drop]


def _deduplicate_reordered_pairs(items: list[DiffItem]) -> list[DiffItem]:
    """Ensure that any text present on both sides as reordered is only listed
    ONCE, anchored to the Original reference position with suspect_location set."""
    added_reorders_by_text: dict[str, list[DiffItem]] = {}
    for i in items:
        if i.kind == "reordered" and not i.approved_text and i.suspect_text:
            added_reorders_by_text.setdefault(i.suspect_text, []).append(i)

    items_to_drop: set[int] = set()
    for item in items:
        if item.kind == "reordered" and item.approved_text and not item.suspect_text:
            matching = added_reorders_by_text.get(item.approved_text)
            if matching:
                add_it = matching.pop(0)
                item.suspect_text = add_it.suspect_text
                item.suspect_location = add_it.suspect_location
                item.suspect_caret = add_it.suspect_caret
                items_to_drop.add(id(add_it))

    return [i for i in items if id(i) not in items_to_drop]


def _location_of(tokens: list[ComparisonToken]) -> Optional[Location]:
    """Per-page, per-line boxes for a run of tokens, plus a primary
    (page, union bbox) for simple/legacy navigation.

    A discrepancy slice can span more than one PAGE, not just many
    lines on one page -- e.g. a paragraph that starts at the bottom of
    one page and wraps onto the next. The previous version of this
    function only looked at `tokens_with_pos[0].page` and silently
    DROPPED every word on any other page, which meant a discrepancy
    spanning a page boundary in a multi-page document (or, in
    practice, any comparison against a "concise" 1-page counterpart of
    a longer document) could end up with an empty/wrong Location that
    never highlighted anywhere. Every page the slice's words actually
    touch is now kept, in `Location.boxes_by_page`.
    """
    tokens_with_pos = [t for t in tokens if t.bbox is not None]
    if not tokens_with_pos:
        return None
    words_by_page: dict[int, list[Word]] = {}
    for t in tokens_with_pos:
        for w in t.source_words:
            words_by_page.setdefault(w.page, []).append(w)
    if not words_by_page:
        return None

    boxes_by_page: dict[int, list[tuple[float, float, float, float]]] = {}
    for page, words in words_by_page.items():
        boxes = _line_boxes(words)
        if boxes:
            boxes_by_page[page] = boxes
    if not boxes_by_page:
        return None

    primary_page = min(boxes_by_page)
    primary_boxes = boxes_by_page[primary_page]
    union = (
        min(b[0] for b in primary_boxes),
        min(b[1] for b in primary_boxes),
        max(b[2] for b in primary_boxes),
        max(b[3] for b in primary_boxes),
    )
    return Location(
        page=primary_page, bbox=union, boxes=primary_boxes, boxes_by_page=boxes_by_page
    )


# A caret's width floors out here so a marker anchored next to a single
# narrow character (a bare "." or "-") is still clearly visible as
# "about one character wide" rather than collapsing to a sliver.
_MIN_CARET_WIDTH = 3.0


def _caret_location(
    tokens: list[ComparisonToken], i1: int, i2: int
) -> Optional[Location]:
    """A marker about ONE CHARACTER wide at the insertion point between
    token ``i1 - 1`` and token ``i1`` in ``tokens`` (``i1 == i2`` for a
    pure insert/delete opcode -- the span is zero-width in this
    stream's OWN document).

    This exists for the side of an insertion/deletion that has no
    Location of its own: the text genuinely isn't there. The previous
    behavior mirrored the OTHER document's full bbox onto this one --
    for a whole added sentence, that meant drawing a sentence-sized
    highlight box on the document that doesn't have the sentence, using
    coordinates from an entirely different layout, which lands on
    arbitrary unrelated content and looks like a mistake. Anchoring
    directly to this document's own neighboring word instead -- right
    after the word before the insertion point, or right before the
    word after it if there's nothing before -- gives a marker of the
    right SCALE (about as wide as one character) at a position that
    actually means something in this document.

    Returns None if neither neighbor has real coordinates (e.g. an
    empty document, or -- for a nested edit inside an already-isolated
    replace span -- the insertion sits at that span's own edge with no
    further context available); callers should fall back to the old
    other-side-bbox behavior in that rare case.
    """
    prev_word = _edge_word(tokens, range(i1 - 1, -1, -1), from_end=True)
    if prev_word is not None:
        char_w = max(_char_width(prev_word), _MIN_CARET_WIDTH)
        box = (prev_word.x1, prev_word.top, prev_word.x1 + char_w, prev_word.bottom)
        return Location(page=prev_word.page, bbox=box, boxes=[box], boxes_by_page={prev_word.page: [box]})

    next_word = _edge_word(tokens, range(i2, len(tokens)), from_end=False)
    if next_word is not None:
        char_w = max(_char_width(next_word), _MIN_CARET_WIDTH)
        # Deliberately NOT clamped to a non-negative x0: a word sitting
        # exactly at the page's left edge would otherwise collapse this
        # box to zero width (max(x0 - char_w, 0.0) == x0 when x0 is
        # already <= char_w), silently breaking the "about one
        # character wide" guarantee for that edge case. A few points of
        # negative coordinate just draws slightly into the margin,
        # which is harmless.
        box = (next_word.x0 - char_w, next_word.top, next_word.x0, next_word.bottom)
        return Location(page=next_word.page, bbox=box, boxes=[box], boxes_by_page={next_word.page: [box]})

    return None


def _edge_word(
    tokens: list[ComparisonToken], indices: range, *, from_end: bool
) -> Optional[Word]:
    """The last (``from_end``) or first source Word of the nearest token
    in ``indices`` that has any positioned source words at all."""
    for idx in indices:
        words = tokens[idx].source_words
        if words:
            return words[-1] if from_end else words[0]
    return None


def _char_width(word: Word) -> float:
    """Rough single-character width for ``word``, from its own bbox --
    self-consistent with whatever font size THIS document actually
    uses, rather than a hardcoded point size that would be wildly wrong
    for, say, a small caption vs. a large heading."""
    text_len = max(len(word.text), 1)
    return (word.x1 - word.x0) / text_len


def _line_boxes(words: list[Word]) -> list[tuple[float, float, float, float]]:
    """One horizontal box per visual line, in top-to-bottom order.

    Words are bucketed by their vertical position (rounded to a small
    tolerance so baseline jitter within one line stays together) and
    each bucket becomes a single horizontal rectangle spanning that
    line's words.
    """
    buckets: dict[int, list[Word]] = {}
    for word in words:
        key = round(word.top / _LINE_BUCKET_TOLERANCE)
        buckets.setdefault(key, []).append(word)
    boxes: list[tuple[float, float, float, float]] = []
    for key in sorted(buckets):
        line_words = buckets[key]
        boxes.append(
            (
                min(w.x0 for w in line_words),
                min(w.top for w in line_words),
                max(w.x1 for w in line_words),
                max(w.bottom for w in line_words),
            )
        )
    return boxes


# ----------------------------------------------------------------------
# MVP-8: out-of-order section matching
# ----------------------------------------------------------------------

@dataclass
class _Section:
    """One chunk of a document: a numbered header plus its body (or the
    preamble before the first header). `tokens` is the full slice --
    header AND body -- so matched sections can be diffed in isolation.
    """

    tokens: list[ComparisonToken]
    title: str  # joined header text; "" for the preamble chunk

    @property
    def text_tokens(self) -> list[str]:
        return [t.text for t in self.tokens]

    @property
    def header_key(self) -> tuple[Optional[str], str]:
        """(section number, title letters) used for TITLE-ANCHORED matching.

        Delegates to ``core.section_diff.section_key`` -- the single key
        normalization shared by the block-level and word-level pipelines.
        The number is the leading integer ("2") or subsection ("2.1")
        kept as a string so subsections match other subsections only; the
        title letters are casefolded with everything non-alphabetic
        stripped, so "4 CONTRAINDICATION S" and "4 CONTRAINDICATIONS"
        normalize to the same key even though a narrow layout split the
        title mid-word. The preamble (no header) yields (None, "").
        """
        return section_key(self.title)


# Minimum similarity (difflib ratio on the section's token texts) for
# two sections to be considered the same content. Greedy best-first
# assignment below this is too likely to pair unrelated sections.
_SECTION_MATCH_RATIO = 0.5

# Minimum letter-sequence similarity for two sections sharing the same
# header NUMBER to be treated as the same section (title-anchored).
# Deliberately permissive -- the number is the strong anchor, and the
# title may be split mid-word by a narrow layout ("4 CONTRAINDICATION"
# + "S" vs "4 CONTRAINDICATIONS").
_SECTION_TITLE_RATIO = 0.4

# Minimum letter-sequence similarity for two UNNUMBERED headers
# (MVP-19: "CARDMEMBER AGREEMENT", "INTEREST CHARGES" -- documents
# with no leading section number to anchor on at all) to be
# title-anchored. Deliberately stricter than _SECTION_TITLE_RATIO
# above: without a number to independently confirm the pairing, the
# title match is the ONLY signal, so it needs to be a close match, not
# just a loose family resemblance -- two genuinely different unnumbered
# sections of the same document are far more likely to share a stray
# few letters than two differently-numbered ones are.
_UNNUMBERED_SECTION_TITLE_RATIO = 0.75


def _group_token_lines(tokens: list[ComparisonToken]) -> list[list[ComparisonToken]]:
    """Group tokens into visual lines by (page, vertical position). Two
    consecutive tokens share a line when their `top`s are within
    `_LINE_BUCKET_TOLERANCE` points (baseline jitter within one line is
    smaller than the line spacing).

    A horizontal-gap check was tried here too (splitting a "line" when
    two tokens at the same height were implausibly far apart
    horizontally, to stop two unrelated running heads that happen to
    land at the same page height on a wide combined-sheet layout from
    concatenating into one nonsense section title) and was reverted:
    verified, on the real document that motivated it, to do far more
    harm than good -- a dense multi-column "term: definition" section
    elsewhere on the SAME document legitimately has within-line gaps
    in that same range, and splitting those apart scrambled roughly 40
    OTHER lines to fix the one it targeted. Left as a documented,
    narrower known limitation instead of a heuristic that trades one
    real bug for a larger one.
    """
    lines: list[list[ComparisonToken]] = []
    for tok in tokens:
        top = tok.source_words[0].top if tok.source_words else None
        bottom = tok.source_words[0].bottom if tok.source_words else None
        page = tok.page
        same_line = False
        if lines and top is not None:
            prev = lines[-1][-1]
            prev_top = prev.source_words[0].top if prev.source_words else None
            prev_bottom = prev.source_words[0].bottom if prev.source_words else None
            if prev_top is not None and page == prev.page:
                if abs(top - prev_top) <= 4.0:
                    same_line = True
                elif prev_bottom is not None and bottom is not None:
                    y_overlap = min(bottom, prev_bottom) - max(top, prev_top)
                    tok_h = max(bottom - top, 1.0)
                    prev_h = max(prev_bottom - prev_top, 1.0)
                    if y_overlap > 0 and (y_overlap / min(tok_h, prev_h)) >= 0.25:
                        same_line = True
        if same_line:
            lines[-1].append(tok)
        else:
            lines.append([tok])
    return lines


def _split_sections(tokens: list[ComparisonToken]) -> Optional[list[_Section]]:
    """Chunk a token stream into sections at headers or paragraphs.

    The token stream is projected onto the block schema of
    ``core.section_diff.build_section_list`` (one block per visual line,
    carrying the line's tokens and its table membership) and chunked
    there, so the header detector is shared with the block-level
    pipeline.

    When at least two headers are detected, returns the header-chunked
    sections. If fewer headers are detected, falls back to paragraph/block
    chunking so multi-paragraph or multi-column documents without
    explicit headers still match position-agnostically across pages and
    columns.

    Returns None only for single-line / trivial documents (where whole-stream
    linear diff is identical).
    """
    section_list = build_section_list(_tokens_to_blocks(tokens))
    if sum(1 for key, _ in section_list if key) >= 2:
        return [
            _Section(
                tokens=[token for block in sec_blocks for token in block["_tokens"]],
                title=key,
            )
            for key, sec_blocks in section_list
        ]
    return _split_paragraphs(tokens)


def _split_paragraphs(tokens: list[ComparisonToken]) -> Optional[list[_Section]]:
    """Chunk a token stream into visual paragraphs / blocks when no
    explicit section headers are present.

    Breaks at page boundaries or double-line gaps between body paragraphs.
    Never splits across table cells/rows.
    """
    lines = _group_token_lines(tokens)
    if len(lines) < 2:
        return None

    # If the entire token stream is inside tables, do not split into fake paragraphs
    if all(
        any(t.source_words and t.source_words[0].cell_id is not None for t in line)
        for line in lines
    ):
        return None

    paragraphs: list[list[ComparisonToken]] = []
    curr_para: list[ComparisonToken] = []
    prev_line = None

    for line in lines:
        if not curr_para:
            curr_para.extend(line)
            prev_line = line
            continue

        is_break = False
        in_table = any(t.source_words and t.source_words[0].cell_id is not None for t in line)
        prev_in_table = (
            any(t.source_words and t.source_words[0].cell_id is not None for t in prev_line)
            if prev_line
            else False
        )

        if prev_line and line:
            prev_page = prev_line[0].page
            curr_page = line[0].page
            if curr_page != prev_page:
                is_break = True
            elif not in_table and not prev_in_table:
                prev_top = prev_line[0].source_words[0].top if prev_line[0].source_words else 0
                prev_bottom = prev_line[0].source_words[0].bottom if prev_line[0].source_words else 0
                curr_top = line[0].source_words[0].top if line[0].source_words else 0
                line_height = max(prev_bottom - prev_top, 8.0)
                if curr_top - prev_bottom >= 1.8 * line_height:
                    is_break = True

        if is_break and len(curr_para) >= 10:
            paragraphs.append(curr_para)
            curr_para = list(line)
        else:
            curr_para.extend(line)
        prev_line = line

    if curr_para:
        paragraphs.append(curr_para)

    if len(paragraphs) < 2:
        return None

    return [_Section(tokens=para, title="") for para in paragraphs]


def _tokens_to_blocks(tokens: list[ComparisonToken]) -> list[dict]:
    """Project a token stream onto ``build_section_map``'s block schema.

    Each visual line of tokens becomes one block dict with the public
    {"page", "bbox", "text"} schema plus two private keys: "_tokens"
    (the line's tokens, so a section's token slice can be reconstructed
    exactly for word-level diffing) and "_in_table" (the line contains
    a table-cell word, so it can never be a section header).
    """
    blocks: list[dict] = []
    for line in _group_token_lines(tokens):
        page = line[0].page if line[0].source_words else 1
        boxes = [t.bbox for t in line if t.bbox is not None]
        if boxes:
            bbox = [
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            ]
        else:
            bbox = [0.0, 0.0, 0.0, 0.0]
        blocks.append(
            {
                "page": page,
                "bbox": bbox,
                "text": " ".join(t.text for t in line),
                "_tokens": line,
                "_in_table": any(
                    t.source_words and t.source_words[0].cell_id is not None
                    for t in line
                ),
            }
        )
    return blocks


def _match_sections(
    sections_a: list[_Section], sections_b: list[_Section]
) -> tuple[dict[int, int], set[int], set[int]]:
    """Global block lookup: match each suspect section to its best
    approved twin, position-agnostic.

    Matching is TITLE-ANCHORED first: two sections sharing the same
    header NUMBER with a similar title ("2 DOSAGE AND ADMINISTRATION"
    vs "2 DOSAGE AND", "4 CONTRAINDICATION S" vs "4
    CONTRAINDICATIONS") are paired regardless of body content. This is
    essential for multi-column leaflets where sections SPAN columns:
    the chunk under a header can absorb other sections' overflow text,
    so body-similarity alone would pair "5 WARNINGS" (which absorbed
    section 2's tail) against "2 DOSAGE". Sections without a matching
    number/title (preamble, unnumbered, renumbered documents) fall back
    to full token-sequence similarity, position-agnostic.

    A header can appear several times in one document -- most often a
    table-of-contents line and the section it points at, but also a
    running head, or a leaflet repeated on a multi-up print sheet. Every
    such copy scores identically on number and title, so the score alone
    cannot say WHICH copy is which, and a contents line would happily
    claim the body section (making its whole body read as deleted, and
    the real body's as added). Ties are therefore broken on CONTEXT
    first: the words immediately before and after the section
    (`_section_edges`). A contents line sits between other contents
    lines; the section itself sits between paragraphs of prose. Only
    when the context is equally good does the n-th occurrence prefer the
    n-th on the other side, as before.

    Two things settle WHICH of several similar chunks a chunk pairs
    with, before similarity is even consulted:

      1. A chunk whose text is IDENTICAL on both sides is paired
         straight away (`_identical_pairs`) and taken out of the search
         entirely -- a 100% match needs no competitor, and skipping it
         is also most of the work saved on a document that repeats
         itself. Repeated identical chunks pair in document order, so
         the first copy pairs with the first copy.
      2. Everything else is matched in the order the documents read.
         Those identical pairs form a monotone backbone
         (`_monotone_backbone`), and a pair that does not fit between
         its neighbouring anchors has to beat the one that does by
         `_OUT_OF_SEQUENCE_PENALTY` to win. Without this, a paragraph
         sharing a few lines of boilerplate with a paragraph elsewhere
         in the document could claim it on a hair's-breadth higher
         similarity, which leaves the real counterpart unmatched and
         reports both as wholly changed. A genuinely relocated
         section still wins when it is clearly the better match, so
         move detection is unaffected.

    Returns:
        (b_to_a, unmatched_a, unmatched_b): b_to_a maps a suspect
        section index to its matched approved index; the sets are the
        sections present on only one side.
    """
    texts_a = [sec.text_tokens for sec in sections_a]
    texts_b = [sec.text_tokens for sec in sections_b]
    keys_a = [sec.header_key for sec in sections_a]
    keys_b = [sec.header_key for sec in sections_b]
    # A header can occur more than once (a table-of-contents line and
    # its section); such chunks tie on title, so the n-th occurrence on
    # one side prefers the n-th on the other.
    rank_a = _occurrence_ranks(keys_a)
    rank_b = _occurrence_ranks(keys_b)

    # 1. Identical text pairs itself up and leaves the search.
    identical = _identical_pairs(texts_a, texts_b)
    b_to_a: dict[int, int] = {b_idx: a_idx for a_idx, b_idx in identical}
    used_a: set[int] = {a_idx for a_idx, _b_idx in identical}
    # 2. Those pairs, reduced to the ones that read in order on both
    #    sides, are the backbone the rest is matched against.
    in_sequence = _SequenceBounds(_monotone_backbone(identical))

    context_ratio = _ContextSimilarity(sections_a, sections_b)
    body_ratio = _BodySimilarity(texts_a, texts_b)
    title_ratio = _TitleSimilarity()
    candidates: list[tuple[float, bool, float, bool, int, int]] = []
    for a_idx, ta in enumerate(texts_a):
        if a_idx in used_a:
            continue
        num_a, title_a = keys_a[a_idx]
        for b_idx, tb in enumerate(texts_b):
            if b_idx in b_to_a:
                continue
            num_b, title_b = keys_b[b_idx]
            score: Optional[float] = None
            t_ratio = 0.0
            if title_a and title_b:
                if num_a is not None and num_a == num_b:
                    # Used as a score: needs the exact value.
                    t_ratio = title_ratio.exact(title_a, title_b)
                else:
                    # Only compared with a threshold below: skip titles
                    # that provably can't reach it.
                    threshold = (
                        _SECTION_TITLE_RATIO
                        if (num_a is not None or num_b is not None)
                        else _UNNUMBERED_SECTION_TITLE_RATIO
                    )
                    t_ratio = title_ratio.bounded(title_a, title_b, threshold)

            if num_a is not None and num_a == num_b:
                if title_a and title_b and t_ratio >= _SECTION_TITLE_RATIO:
                    # Same number AND matching title (e.g. "2.1 Dosage" vs "2.1 DOSAGE")
                    score = 2.0 + t_ratio
                elif not title_a or not title_b:
                    # One or both lack titles: number is sole anchor
                    score = 1.5
                else:
                    # Same number, but titles differ ("5.1 Diabetic Ketoacidosis" vs "5.1 Pancreatitis").
                    # Only pair by number if body content actually confirms they are the same section!
                    b_ratio = body_ratio(a_idx, b_idx)
                    if b_ratio >= 0.5:
                        score = 1.0 + b_ratio
            elif title_a and title_b:
                # Different numbers (or one/both unnumbered), but titles match!
                # Catches renumbered subsections (e.g. "5.2 Pancreatitis" <-> "5.1 Pancreatitis")
                # and unnumbered sections ("CARDMEMBER AGREEMENT", "Your Billing Rights:").
                threshold = (
                    _SECTION_TITLE_RATIO
                    if (num_a is not None or num_b is not None)
                    else _UNNUMBERED_SECTION_TITLE_RATIO
                )
                if t_ratio >= threshold:
                    b_ratio = body_ratio(a_idx, b_idx)
                    if b_ratio >= _SECTION_MATCH_RATIO or t_ratio >= 0.7:
                        score = 1.5 + t_ratio
            if score is None:
                ratio = body_ratio(a_idx, b_idx)
                if ratio >= _SECTION_MATCH_RATIO:
                    score = ratio
            if score is not None:
                # A pair that does not read in order has to be clearly
                # better than one that does. Then the tie-breakers: the
                # surrounding text, then the n-th-occurrence
                # preference. Both are kept OUT of the score so they
                # can only choose between equally-scoring pairs.
                ordered = in_sequence(a_idx, b_idx)
                candidates.append(
                    (
                        score if ordered else score - _OUT_OF_SEQUENCE_PENALTY,
                        ordered,
                        context_ratio(a_idx, b_idx),
                        rank_a[a_idx] == rank_b[b_idx],
                        # Last word goes to document order: of two
                        # copies that are otherwise indistinguishable
                        # (the same paragraph repeated on every page),
                        # the earlier one is the one a reader means.
                        -a_idx,
                        -b_idx,
                    )
                )
    # best-first: score (order-adjusted), in order, context, occurrence,
    # then earliest in the document
    candidates.sort(reverse=True)

    for _score, _ordered, _context, _same_rank, neg_a, neg_b in candidates:
        a_idx, b_idx = -neg_a, -neg_b
        if b_idx in b_to_a or a_idx in used_a:
            continue
        b_to_a[b_idx] = a_idx
        used_a.add(a_idx)
    unmatched_a = set(range(len(sections_a))) - used_a
    unmatched_b = set(range(len(sections_b))) - set(b_to_a)
    return b_to_a, unmatched_a, unmatched_b


# How much better an out-of-order pair must score to beat one that
# reads in order (see `_match_sections`). Small enough that a section
# genuinely relocated -- which is a much better match than anything in
# its place -- still wins and is still reported as moved; large enough
# that two paragraphs sharing boilerplate cannot swap partners on a
# fractionally higher similarity.
_OUT_OF_SEQUENCE_PENALTY = 0.15


def _identical_pairs(
    texts_a: list[list[str]], texts_b: list[list[str]]
) -> list[tuple[int, int]]:
    """(approved, suspect) index pairs whose text is exactly the same.

    A 100% match is the end of the question: the pair is taken and
    neither side is compared with anything else, which is both the
    right answer and the cheapest one -- on a document that repeats
    itself, this is most of the pairs, and each one removed takes a
    whole row and column out of the similarity search.

    Only text occurring ONCE on each side is anchored here, because
    only then is there nothing to get wrong. Text repeated down a
    document -- a boilerplate warning, a bare header, a leaflet printed
    several times -- is identical to every other copy of itself, so
    "identical" cannot say which copy belongs to which; worse, the
    copies that happen to be identical in one document are often not
    the same ones in the other (a leaflet whose third copy was edited),
    so pairing them off in order would hand out partners pages away
    from where they belong. Those are left to the scored pass, where
    the surrounding text and the document order decide.
    """
    by_text_a: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for index, text in enumerate(texts_a):
        if text:
            by_text_a[tuple(text)].append(index)
    by_text_b: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for index, text in enumerate(texts_b):
        if text:
            by_text_b[tuple(text)].append(index)
    return [
        (a_indices[0], by_text_b[text][0])
        for text, a_indices in by_text_a.items()
        if len(a_indices) == 1 and len(by_text_b.get(text, ())) == 1
    ]


def _monotone_backbone(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The largest subset of ``pairs`` that reads in order in BOTH
    documents -- the part of the alignment that is certainly not a move.

    Identical text can also be text that MOVED, and one moved copy must
    not drag the order check off course, so the backbone is the longest
    increasing run rather than every pair (the classic patience-diff
    anchor set).
    """
    if not pairs:
        return []
    # One pair per suspect chunk (`_identical_pairs` already guarantees
    # this; kept so the backbone is well-defined for any input).
    ordered: list[tuple[int, int]] = []
    for pair in sorted(pairs, key=lambda pair: (pair[1], pair[0])):
        if not ordered or ordered[-1][1] != pair[1]:
            ordered.append(pair)
    # Patience sort over the approved indices: `tails[k]` is the
    # smallest approved index that can end an increasing run of k+1.
    tails: list[int] = []
    tail_pairs: list[tuple[int, int]] = []
    previous: list[int] = []  # index in `ordered` of the pair before each one
    positions: list[int] = []  # index in `ordered` of each tail's pair
    for position, (a_idx, _b_idx) in enumerate(ordered):
        slot = bisect.bisect_left(tails, a_idx)
        previous.append(positions[slot - 1] if slot > 0 else -1)
        if slot == len(tails):
            tails.append(a_idx)
            positions.append(position)
            tail_pairs.append(ordered[position])
        else:
            tails[slot] = a_idx
            positions[slot] = position
            tail_pairs[slot] = ordered[position]
    backbone: list[tuple[int, int]] = []
    position = positions[-1] if positions else -1
    while position >= 0:
        backbone.append(ordered[position])
        position = previous[position]
    backbone.reverse()
    return backbone


class _SequenceBounds:
    """Whether a pair fits the document order the backbone establishes.

    A chunk between two anchored chunks must pair with something
    between their partners; anything else is reading out of order. With
    no anchors at all (two documents with nothing identical in them)
    every pair is in order, so the order rule simply does not apply.
    """

    def __init__(self, backbone: list[tuple[int, int]]) -> None:
        self._approved = [a_idx for a_idx, _b_idx in backbone]
        self._suspect = [b_idx for _a_idx, b_idx in backbone]

    def __call__(self, a_idx: int, b_idx: int) -> bool:
        slot = bisect.bisect_left(self._suspect, b_idx)
        low = self._approved[slot - 1] if slot > 0 else -1
        high = self._approved[slot] if slot < len(self._approved) else None
        return low < a_idx and (high is None or a_idx < high)


# Words of surrounding text used to tell two copies of the same header
# apart (`_ContextSimilarity`). Long enough to reach past a neighboring
# header into its body -- which is exactly what distinguishes a contents
# line (surrounded by other contents lines) from the section itself
# (surrounded by prose) -- and short enough to stay cheap.
_CONTEXT_TOKENS = 12


def _section_edges(
    sections: list["_Section"],
) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
    """(before, after) for each section: the `_CONTEXT_TOKENS` words
    immediately preceding it and immediately following it in the
    document, walking across neighboring sections as needed."""
    texts = [sec.text_tokens for sec in sections]
    before: list[tuple[str, ...]] = []
    after: list[tuple[str, ...]] = []
    for i in range(len(sections)):
        acc: list[str] = []
        j = i - 1
        while j >= 0 and len(acc) < _CONTEXT_TOKENS:
            acc = texts[j][-(_CONTEXT_TOKENS - len(acc)) :] + acc
            j -= 1
        before.append(tuple(acc))
        acc = []
        j = i + 1
        while j < len(sections) and len(acc) < _CONTEXT_TOKENS:
            acc = acc + texts[j][: _CONTEXT_TOKENS - len(acc)]
            j += 1
        after.append(tuple(acc))
    return before, after


class _ContextSimilarity:
    """How alike two sections' SURROUNDINGS are, in [0, 1].

    The tie-breaker between copies of the same header (see
    `_match_sections`): the text before and after a contents line is
    other contents lines, the text before and after the real section is
    prose, so the real pairing wins on context even though both copies
    score identically on number and title.

    A section genuinely moved elsewhere has different neighbors and
    scores 0 here -- which is why this only ever breaks TIES, never
    outranks a better number/title/body match.
    """

    def __init__(self, sections_a: list["_Section"], sections_b: list["_Section"]) -> None:
        self._before_a, self._after_a = _section_edges(sections_a)
        self._before_b, self._after_b = _section_edges(sections_b)
        # A repeated document repeats its surroundings too, so the same
        # two word sequences are compared many times over.
        self._cache: dict[tuple[tuple[str, ...], tuple[str, ...]], float] = {}

    def __call__(self, a_idx: int, b_idx: int) -> float:
        return 0.5 * (
            self._ratio(self._before_a[a_idx], self._before_b[b_idx])
            + self._ratio(self._after_a[a_idx], self._after_b[b_idx])
        )

    def _ratio(self, a: tuple[str, ...], b: tuple[str, ...]) -> float:
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        value = self._cache.get((a, b))
        if value is None:
            value = self._cache[(a, b)] = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
        return value


class _TitleSimilarity:
    """difflib ratio of two section TITLES, memoized by the title pair.

    A document repeats its headers -- a contents list and the sections
    it points at, a leaflet printed several times on one sheet -- so a
    few dozen distinct titles generate hundreds of thousands of section
    pairs, and comparing the same two strings again is most of section
    matching's time. The cache makes each distinct comparison happen
    once; results are unchanged.
    """

    def __init__(self) -> None:
        self._bags: dict[str, Counter] = {}
        self._cache: dict[tuple[str, str, Optional[float]], float] = {}

    def exact(self, title_a: str, title_b: str) -> float:
        """The real ratio (the caller uses the value itself)."""
        key = (title_a, title_b, None)
        value = self._cache.get(key)
        if value is None:
            value = self._cache[key] = difflib.SequenceMatcher(None, title_a, title_b).ratio()
        return value

    def bounded(self, title_a: str, title_b: str, threshold: float) -> float:
        """The ratio when it can reach ``threshold``, else 0.0."""
        key = (title_a, title_b, threshold)
        value = self._cache.get(key)
        if value is None:
            value = self._cache[key] = _bounded_ratio(
                title_a, title_b, self._bag(title_a), self._bag(title_b), threshold
            )
        return value

    def _bag(self, title: str) -> Counter:
        bag = self._bags.get(title)
        if bag is None:
            bag = self._bags[title] = Counter(title)
        return bag


def _bounded_ratio(a: str, b: str, bag_a: Counter, bag_b: Counter, threshold: float) -> float:
    """difflib ratio of ``a`` and ``b`` when it can reach ``threshold``,
    else 0.0 (checked first against the length and shared-character
    upper bounds)."""
    total = len(a) + len(b)
    if 2 * min(len(a), len(b)) < threshold * total:
        return 0.0
    shared = sum(min(count, bag_b[ch]) for ch, count in bag_a.items() if ch in bag_b)
    if 2 * shared < threshold * total:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


class _BodySimilarity:
    """difflib ratio of two sections' token lists, for `_match_sections`.

    Every use compares the ratio against `_SECTION_MATCH_RATIO` (0.5)
    before using it, so pairs that provably can't reach it return 0.0
    without the expensive match: the ratio can never exceed the
    length bound 2*min/(la+lb), nor the shared-word bound
    2*|A&B|/(la+lb) (difflib's own real_quick_ratio / quick_ratio).
    With a hundred-plus sections per document that skips most of the
    ~n^2 pairs. The b-side analysis is also built once per section
    instead of once per pair. Results are identical to calling
    SequenceMatcher(None, ta, tb, autojunk=False).ratio() whenever that
    ratio is >= the threshold.
    """

    def __init__(self, texts_a: list[list[str]], texts_b: list[list[str]]) -> None:
        self._texts_a = texts_a
        self._texts_b = texts_b
        self._bags_a = [Counter(t) for t in texts_a]
        self._bags_b = [Counter(t) for t in texts_b]
        self._matchers: dict[int, difflib.SequenceMatcher] = {}

    def __call__(self, a_idx: int, b_idx: int) -> float:
        ta, tb = self._texts_a[a_idx], self._texts_b[b_idx]
        total = len(ta) + len(tb)
        if not total:
            return 1.0
        if 2 * min(len(ta), len(tb)) < _SECTION_MATCH_RATIO * total:
            return 0.0
        bag_a, bag_b = self._bags_a[a_idx], self._bags_b[b_idx]
        if len(bag_a) > len(bag_b):
            bag_a, bag_b = bag_b, bag_a
        shared = sum(min(count, bag_b[word]) for word, count in bag_a.items() if word in bag_b)
        if 2 * shared < _SECTION_MATCH_RATIO * total:
            return 0.0
        matcher = self._matchers.get(b_idx)
        if matcher is None:
            matcher = difflib.SequenceMatcher(None, [], tb, autojunk=False)
            self._matchers[b_idx] = matcher
        matcher.set_seq1(ta)
        return matcher.ratio()


def _occurrence_ranks(keys: list[tuple[Optional[str], str]]) -> list[int]:
    seen: Counter = Counter()
    ranks = []
    for key in keys:
        ranks.append(seen[key])
        seen[key] += 1
    return ranks


def _same_title(sec_a: _Section, sec_b: _Section) -> bool:
    """Whether two paired sections carry the same header (as opposed to
    being paired by body content alone)."""
    if sec_a.header_key == sec_b.header_key:
        return True  # includes two preambles
    _num_a, title_a = sec_a.header_key
    _num_b, title_b = sec_b.header_key
    # Similar titles count even when renumbered ("5.2 Pancreatitis" ->
    # "5.1 Pancreatitis").
    return bool(title_a and title_b) and (
        difflib.SequenceMatcher(None, title_a, title_b).ratio() >= _SECTION_TITLE_RATIO
    )


def _longest_increasing_subsequence_indices(seq: list[int]) -> set[int]:
    """Return the set of indices in `seq` that belong to the longest strictly
    increasing subsequence. Elements at indices NOT in this set are the ones
    that broke monotonicity (i.e. moved).
    """
    if not seq:
        return set()
    n = len(seq)
    tails = []
    prev = [-1] * n

    for i, x in enumerate(seq):
        lo, hi = 0, len(tails)
        while lo < hi:
            mid = (lo + hi) // 2
            if seq[tails[mid]] < x:
                lo = mid + 1
            else:
                hi = mid
        if lo < len(tails):
            tails[lo] = i
        else:
            tails.append(i)
        if lo > 0:
            prev[i] = tails[lo - 1]

    lis_indices = set()
    curr = tails[-1] if tails else -1
    while curr != -1:
        lis_indices.add(curr)
        curr = prev[curr]
    return lis_indices


def _moved_section_pairs(b_to_a: dict[int, int]) -> set[int]:
    """Approved section indices whose matched suspect twin sits out of
    order -- a genuine section MOVE, not a content change.

    Pairs are walked in approved order; the subset of suspect positions that
    preserved monotonicity forms the Longest Increasing Subsequence (LIS).
    Any section not in the LIS is the displaced (moved) one: approved [A B C D]
    vs suspect [D A B C] gives suspect positions 1,2,3,0 -> LIS is 1,2,3 -> D is moved.
    """
    pairs = sorted((a_idx, b_idx) for b_idx, a_idx in b_to_a.items())
    seq = [b_idx for _, b_idx in pairs]
    lis = _longest_increasing_subsequence_indices(seq)
    return {pairs[i][0] for i in range(len(pairs)) if i not in lis}


def _has_matching_context(
    span_a: list[ComparisonToken],
    span_b: list[ComparisonToken],
    tokens_a: list[ComparisonToken],
    tokens_b: list[ComparisonToken],
    k: int = 3,
) -> bool:
    """Check if span_a and span_b share either preceding or succeeding context
    in the full token streams. If they do, they occupy the same relative position
    in the document flow and are not a genuine relocation."""
    try:
        id_a_start = id(span_a[0])
        id_a_end = id(span_a[-1])
        id_b_start = id(span_b[0])
        id_b_end = id(span_b[-1])

        idx_a_start = next(i for i, t in enumerate(tokens_a) if id(t) == id_a_start)
        idx_a_end = next(i for i, t in enumerate(tokens_a) if id(t) == id_a_end)
        idx_b_start = next(i for i, t in enumerate(tokens_b) if id(t) == id_b_start)
        idx_b_end = next(i for i, t in enumerate(tokens_b) if id(t) == id_b_end)
    except (StopIteration, IndexError):
        return False

    def norm_toks(toks: list[ComparisonToken]) -> list[str]:
        words = []
        for t in toks:
            clean = "".join(ch for ch in t.text if ch.isalnum()).casefold()
            if clean:
                words.append(clean)
        return words

    # Check succeeding tokens
    succ_a = norm_toks(tokens_a[idx_a_end + 1 : idx_a_end + 15])[:k]
    succ_b = norm_toks(tokens_b[idx_b_end + 1 : idx_b_end + 15])[:k]
    if len(succ_a) >= k and len(succ_b) >= k and succ_a == succ_b:
        return True

    # Check preceding tokens
    prec_a = norm_toks(tokens_a[max(0, idx_a_start - 15) : idx_a_start])[-k:]
    prec_b = norm_toks(tokens_b[max(0, idx_b_start - 15) : idx_b_start])[-k:]
    if len(prec_a) >= k and len(prec_b) >= k and prec_a == prec_b:
        return True

    return False


def _diff_by_sections(
    approved_tokens: list[ComparisonToken],
    suspect_tokens: list[ComparisonToken],
    sections_a: list[_Section],
    sections_b: list[_Section],
    matches: Optional[TokenMatches] = None,
) -> list[DiffItem]:
    """Per-section comparison for documents with detected headers.

    1. Match each suspect section globally against every approved
       section (position-agnostic).
    2. Diff matched pairs in isolation.
    3. Pair text the section pairing left unexplained but that exists
       on both sides (see the module note, step 5) and diff the rest
       again without it -- genuine internal edits are caught at their
       real severity; identical content produces nothing.
    4. Report each position-rank change as ONE low-severity
       "reordered_section" item and each moved passage as ONE
       "reordered" item (never critical removed/added noise).
    5. Section text present on only one side becomes removed/added
       items -- never a cascade.
    """
    b_to_a, unmatched_a, unmatched_b = _match_sections(sections_a, sections_b)
    moved_sections = _moved_section_pairs(b_to_a)
    pairs = sorted((a, b) for b, a in b_to_a.items())

    # Pass 1: what each section pairing leaves unexplained.
    pair_opcodes = {}
    leftover_a: list[list[ComparisonToken]] = []
    leftover_b: list[list[ComparisonToken]] = []
    for a_idx, b_idx in pairs:
        tokens_a, tokens_b = sections_a[a_idx].tokens, sections_b[b_idx].tokens
        opcodes = _opcodes(tokens_a, tokens_b)
        pair_opcodes[a_idx] = opcodes
        for tag, i1, i2, j1, j2 in opcodes:
            if tag != "equal":
                if i2 > i1:
                    leftover_a.append(tokens_a[i1:i2])
                if j2 > j1:
                    leftover_b.append(tokens_b[j1:j2])
    leftover_a.extend(sections_a[i].tokens for i in sorted(unmatched_a))
    leftover_b.extend(sections_b[i].tokens for i in sorted(unmatched_b))

    # Pass 2: the same text on both sides, in sections that weren't
    # paired. Each moved passage (with any small edits inside it) is
    # compared as a unit and left out of the section comparison.
    passages = _moved_passages(_find_moved_blocks(leftover_a, leftover_b), leftover_a, leftover_b)
    moved_a = {id(t) for span_a, _ in passages for t in span_a}
    moved_b = {id(t) for _, span_b in passages for t in span_b}

    items: list[DiffItem] = []
    for a_idx, b_idx in pairs:
        sec_a, sec_b = sections_a[a_idx], sections_b[b_idx]
        tokens_a = [t for t in sec_a.tokens if id(t) not in moved_a]
        tokens_b = [t for t in sec_b.tokens if id(t) not in moved_b]
        if len(tokens_a) == len(sec_a.tokens) and len(tokens_b) == len(sec_b.tokens):
            opcodes = pair_opcodes[a_idx]  # untouched by pass 2
        else:
            opcodes = _opcodes(tokens_a, tokens_b)
        items.extend(_items_from_opcodes(tokens_a, tokens_b, opcodes, matches))
        # Sections paired only by content (different headers -- e.g. a
        # figure label read as a header) aren't reported as moved: that
        # would claim a section move the documents don't have.
        if a_idx in moved_sections and _same_title(sec_a, sec_b):
            items.append(
                DiffItem(
                    index=len(items),
                    kind="reordered_section",
                    severity="reorder",
                    approved_text=sec_a.title,
                    suspect_text=sec_b.title,
                    approved_location=_location_of(sec_a.tokens),
                    suspect_location=_location_of(sec_b.tokens),
                )
            )

    for a_idx in sorted(unmatched_a):
        for run in _runs_without(sections_a[a_idx].tokens, moved_a):
            items.append(
                DiffItem(
                    index=len(items),
                    kind="removed_in_suspect",
                    severity=_classify_severity("delete", run, []),
                    approved_text=" ".join(t.text for t in run),
                    suspect_text="",
                    approved_location=_location_of(run),
                    suspect_location=None,
                )
            )
    for b_idx in sorted(unmatched_b):
        for run in _runs_without(sections_b[b_idx].tokens, moved_b):
            items.append(
                DiffItem(
                    index=len(items),
                    kind="added_in_suspect",
                    severity=_classify_severity("insert", [], run),
                    approved_text="",
                    suspect_text=" ".join(t.text for t in run),
                    approved_location=None,
                    suspect_location=_location_of(run),
                )
            )

    for span_a, span_b in passages:
        items.extend(_diff_runs(span_a, span_b, matches))  # edits inside the moved text
        if not _has_matching_context(span_a, span_b, approved_tokens, suspect_tokens):
            items.append(
                DiffItem(
                    index=len(items),
                    kind="reordered",
                    severity="reorder",
                    approved_text=" ".join(t.text for t in span_a),
                    suspect_text=" ".join(t.text for t in span_b),
                    approved_location=_location_of(span_a),
                    suspect_location=_location_of(span_b),
                )
            )

    return _finalize_items(items, approved_tokens, suspect_tokens)


# A moved passage must be at least this many identical words: shorter
# runs ("of the patient") recur everywhere and would pair unrelated text.
_MIN_MOVED_RUN_WORDS = 6
# Candidate positions examined per phrase (repeated boilerplate).
_MAX_MOVE_CANDIDATES = 25


def _find_moved_blocks(
    runs_a: list[list[ComparisonToken]], runs_b: list[list[ComparisonToken]]
) -> list[tuple[list[ComparisonToken], list[ComparisonToken]]]:
    """Maximal runs of identical words present in both ``runs_a`` and
    ``runs_b`` (each a list of contiguous token runs), at least
    `_MIN_MOVED_RUN_WORDS` long. Order-independent, so passages that
    swapped places are found too; each token is used at most once.
    A phrase occurring at several places on the other side goes to the
    occurrence giving the longest match."""
    k = _MIN_MOVED_RUN_WORDS
    texts_b = [[t.text for t in run] for run in runs_b]
    index: dict[tuple[str, ...], list[tuple[int, int]]] = {}
    for r, texts in enumerate(texts_b):
        for p in range(len(texts) - k + 1):
            index.setdefault(tuple(texts[p : p + k]), []).append((r, p))
    used_b: set[tuple[int, int]] = set()

    blocks = []
    for run_a in runs_a:
        texts_a = [t.text for t in run_a]
        p = 0
        while p + k <= len(texts_a):
            best_len, best = 0, None
            for r, q in index.get(tuple(texts_a[p : p + k]), ())[:_MAX_MOVE_CANDIDATES]:
                length = 0
                while (
                    p + length < len(texts_a)
                    and q + length < len(texts_b[r])
                    and texts_a[p + length] == texts_b[r][q + length]
                    and (r, q + length) not in used_b
                ):
                    length += 1
                if length > best_len:
                    best_len, best = length, (r, q)
            if best_len < k:
                p += 1
                continue
            r, q = best
            used_b.update((r, q + i) for i in range(best_len))
            blocks.append((run_a[p : p + best_len], runs_b[r][q : q + best_len]))
            p += best_len
    return blocks


# Moved fragments at most this many words apart (on both sides) are one
# moved passage with small edits inside -- reported as one row; the
# edits themselves still get their own rows.
_MAX_MOVED_FRAGMENT_GAP = 5


def _moved_passages(
    blocks: list[tuple[list[ComparisonToken], list[ComparisonToken]]],
    runs_a: list[list[ComparisonToken]],
    runs_b: list[list[ComparisonToken]],
) -> list[tuple[list[ComparisonToken], list[ComparisonToken]]]:
    """Join moved blocks that follow each other closely in BOTH
    documents (same run, gap <= `_MAX_MOVED_FRAGMENT_GAP` words) into
    passages: the contiguous span on each side from the first block's
    start to the last block's end, gaps (the edits) included."""
    position_a = {id(t): (r, i) for r, run in enumerate(runs_a) for i, t in enumerate(run)}
    position_b = {id(t): (r, i) for r, run in enumerate(runs_b) for i, t in enumerate(run)}

    def span(block, position):
        run, start = position[id(block[0])]
        return run, start, start + len(block)

    ordered = sorted(blocks, key=lambda blk: position_a[id(blk[0][0])])
    # [run_a, start_a, end_a, run_b, start_b, end_b] per passage
    spans: list[list[int]] = []
    for block_a, block_b in ordered:
        run_a, start_a, end_a = span(block_a, position_a)
        run_b, start_b, end_b = span(block_b, position_b)
        last = spans[-1] if spans else None
        if (
            last is not None
            and last[0] == run_a
            and last[3] == run_b
            and 0 <= start_a - last[2] <= _MAX_MOVED_FRAGMENT_GAP
            and 0 <= start_b - last[5] <= _MAX_MOVED_FRAGMENT_GAP
        ):
            last[2], last[5] = end_a, end_b
        else:
            spans.append([run_a, start_a, end_a, run_b, start_b, end_b])
    return [
        (runs_a[ra][sa:ea], runs_b[rb][sb:eb]) for ra, sa, ea, rb, sb, eb in spans
    ]


def _runs_without(
    tokens: list[ComparisonToken], excluded: set[int]
) -> list[list[ComparisonToken]]:
    """Contiguous runs of ``tokens`` whose ids aren't in ``excluded``."""
    runs: list[list[ComparisonToken]] = []
    current: list[ComparisonToken] = []
    for token in tokens:
        if id(token) in excluded:
            if current:
                runs.append(current)
                current = []
        else:
            current.append(token)
    if current:
        runs.append(current)
    return runs


# A removed/added run must contain at least this many words before it
# can be reclassified as relocated content -- single common words ("the",
# "mg") appear everywhere and would mass-demote genuine edits.
_MIN_RELOCATION_RUN_WORDS = 3


def _downgrade_relocated_runs(
    items: list[DiffItem],
    approved_tokens: list[ComparisonToken],
    suspect_tokens: list[ComparisonToken],
) -> None:
    """Demote multi-word runs that exist VERBATIM elsewhere in the other
    document to "reorder" -- layout relocation, not content change.

    Multi-column leaflets interleave sections across columns (section
    2's tail printed in column 3 AFTER sections 4 and 5, section 3's
    body in a side panel with no header of its own), so a section's
    chunk can absorb another section's overflow. The absorbed content is
    a CONTIGUOUS multi-word run that exists word-for-word in the other
    document's own section -- but as cleanly-matched content, so the
    exact-text pairing pass cannot see it. This pass checks each
    multi-word removed/added run AND each side of a multi-word replace
    against the other document's full text: if the run appears verbatim
    (or with minor whitespace artifacts) there, the content is present
    on both sides (just relocated by layout), so it is demoted to the
    lowest "reorder" tier instead of a false critical finding.

    Runs that differ by even one word stay untouched, so genuine edits
    inside a relocated span remain critical. Single/common words are
    exempt (see `_MIN_RELOCATION_RUN_WORDS`).
    """
    approved_full = " ".join(t.text for t in approved_tokens)
    suspect_full = " ".join(t.text for t in suspect_tokens)
    approved_alnum = "".join(ch for ch in approved_full if ch.isalnum()).casefold()
    suspect_alnum = "".join(ch for ch in suspect_full if ch.isalnum()).casefold()
    # Every relocation test below is COUNT-based, never a bare "appears
    # somewhere" check: text only counts as relocated when the other
    # document holds at least as many copies of it as this one does. A
    # substring/membership test demoted genuine edits -- an added
    # "daily" (present elsewhere in the approved text), "especially" ->
    # "especial y" ("especial" is a substring of "especially"), "one" ->
    # "ONE" -- all silently became "reorder" and were hidden by the
    # results table's "Ignore minor and below" filter.
    approved_words = Counter(_relocation_word(t.text) for t in approved_tokens)
    suspect_words = Counter(_relocation_word(t.text) for t in suspect_tokens)
    approved_side = (approved_full, approved_alnum, approved_words)
    suspect_side = (suspect_full, suspect_alnum, suspect_words)

    for item in items:
        # For word replacements (text_changed), genuine numeric edits (e.g. 10 mg -> 5 mg) must stay critical
        if item.kind == "text_changed":
            app_digits = _extract_digit_runs(item.approved_text)
            sus_digits = _extract_digit_runs(item.suspect_text)
            if app_digits != sus_digits and (app_digits or sus_digits):
                continue

            app_w = _relocation_word(item.approved_text)
            sus_w = _relocation_word(item.suspect_text)
            # Same word modulo case/punctuation/spacing is an in-place
            # edit (major/minor/typo), never a relocation.
            if _strip_punct(app_w).replace(" ", "") == _strip_punct(sus_w).replace(" ", ""):
                continue
            if (
                " " not in app_w
                and " " not in sus_w
                and len(app_w) >= 3
                and len(sus_w) >= 3
                and suspect_words[app_w] >= approved_words[app_w]
                and approved_words[sus_w] >= suspect_words[sus_w]
            ):
                item.kind = "reordered"
                item.severity = "reorder"
                continue

        # (text, its own document, the other document)
        if item.kind == "removed_in_suspect":
            candidates = [(item.approved_text, approved_side, suspect_side)]
        elif item.kind == "added_in_suspect":
            candidates = [(item.suspect_text, suspect_side, approved_side)]
        elif item.kind == "text_changed":
            candidates = [
                (item.approved_text, approved_side, suspect_side),
                (item.suspect_text, suspect_side, approved_side),
            ]
        else:
            continue
        for text, (own_full, own_alnum, own_words), (other_full, other_alnum, other_words) in candidates:
            word_count = text.count(" ") + 1
            text_alnum = "".join(ch for ch in text if ch.isalnum()).casefold()
            if word_count >= _MIN_RELOCATION_RUN_WORDS and (
                other_full.count(text) >= own_full.count(text) > 0
                or (
                    len(text_alnum) >= 12
                    and other_alnum.count(text_alnum) >= own_alnum.count(text_alnum) > 0
                )
            ):
                item.kind = "reordered"
                item.severity = "reorder"
                break
            # Check constituent words for multi-column permutations (non-numeric text only)
            if not _contains_digit(text):
                words = {w for w in (_relocation_word(t) for t in text.split()) if len(w) >= 3}
                if words and all(other_words[w] >= own_words[w] for w in words):
                    item.kind = "reordered"
                    item.severity = "reorder"
                    break


def _relocation_word(text: str) -> str:
    """Normalize a token/phrase for relocation counting."""
    return text.strip("•.,()").casefold()


_MAX_SLUG_WORDS = 12
_MIN_AXIS_NUMBER_SHARE = 0.6


def _is_figure_artwork_text(text: str) -> bool:
    """Identify vector plot/chart axis labels, tick sequences, chemical diagram atom symbols, or margin slugs."""
    tokens = text.split()
    if not tokens:
        return False
    # 1. Chemical structure atom labels & molecular formulas: e.g. "O HC 3 N N F...", "F N O.", "FNO."
    atom_set = {"H", "C", "N", "O", "F", "P", "S", "CL", "BR", "HC", "CH", "H3C", "CH3", "OH", "NH", "NH2"}
    clean = [t.upper().strip(".,;:()") for t in tokens]
    if clean and any(t in atom_set or (len(t) >= 2 and all(ch in "HCNOFPS" for ch in t)) for t in clean):
        if all(t in atom_set or t.isdecimal() or (len(t) >= 2 and all(ch in "HCNOFPS" for ch in t)) for t in clean):
            return True
    # 2. Production slug with part numbers: e.g. "5322-2026-02 UPADACITINIB EXTENDED-RELEASE TABLETS 2075864-06", "1202-2025-06 Rivaroxaban For Oral Suspension 2xxxxxx", "2xxxxxx"
    if len(tokens) <= _MAX_SLUG_WORDS and (
        any(re.search(r"\d{4,}-\d{4,}-\d+|\d{7}-\d{2}|\d+x{3,}", t, re.I) for t in tokens)
        or (len(tokens) == 1 and re.match(r"^\d+x+$", tokens[0], re.I))
    ):
        return True
    # 3. Plot axis tick sequences / syringe graduations: e.g. "100 90 80 70 60 50 40 30 20 10 0" or "0.9 0.8 1.1 1 1.4..."
    def _is_num_or_unit(t: str) -> bool:
        t_clean = t.rstrip(".,;:")
        return bool(re.match(r"^\d+(\.\d+)?%?$", t_clean)) or t_clean.lower() in {"ml", "mg", "l", "mcg", "g"}

    num_tokens = [t for t in tokens if _is_num_or_unit(t)]
    if len(num_tokens) >= 5 and len(num_tokens) >= len(tokens) * _MIN_AXIS_NUMBER_SHARE:
        return True
    digits = [int(t) for t in tokens if t.isdecimal() and len(t) <= 3]
    if len(digits) >= 5 and len(digits) >= len(tokens) * _MIN_AXIS_NUMBER_SHARE:
        diffs = [b - a for a, b in zip(digits, digits[1:])]
        neg_count = sum(1 for d in diffs if d < 0)
        pos_count = sum(1 for d in diffs if d > 0)
        if neg_count >= 4 or pos_count >= 4:
            return True
    # 4. Repeated chart axis label tokens / forest plot text: "Percent Responders Response Rate (%) ...", "PK Fold Change and 90% CI..."
    chart_keywords = {
        "responders", "response", "rate", "percent", "weeks", "trial", "ad-1", "ad-2",
        "fold", "change", "cmax", "auc", "pk", "postdialysis", "ci", "cl", "normal",
        "severe", "moderate", "mild", "impairment", "reference", "population", "description",
        "interacting", "inhibitor", "drug"
    }
    chart_words = [
        t.lower().strip("()%:†‡§*/") for t in tokens
        if any(t.lower().strip("()%:†‡§*/") == k or k in t.lower() for k in chart_keywords)
        or _is_num_or_unit(t)
    ]
    if len(tokens) >= 4 and len(chart_words) / len(tokens) >= 0.4:
        return True
    # 5. Device / package illustration diagram callouts (e.g. "Cap Bottle adaptor", "SLOW!", "bottle adaptor")
    device_callout_words = {"adaptor", "plunger", "syringe", "bottle", "cap", "slow", "blister", "vial", "needle", "pouch"}
    callout_matches = sum(1 for t in tokens if t.lower().strip("!.:,") in device_callout_words)
    if callout_matches >= 1 and (len(tokens) <= 3 or (len(tokens) <= 10 and callout_matches / len(tokens) >= 0.25)):
        return True
    return False


_HYPHEN_CHARS_CLASS = r"[\-\u2010\u2011\u2012\u2013\u2014\u2212\u00ad]"
_HYPHEN_RE = re.compile(_HYPHEN_CHARS_CLASS)
_REPEATED_HYPHENS_RE = re.compile(_HYPHEN_CHARS_CLASS + r"{3,}")


def _alnum_only(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


def _is_hyphenation(approved: str, suspect: str) -> bool:
    """True when the discrepancy is specifically an added, removed, or
    differently-spaced hyphen/dash between words/numbers, rather than a general
    punctuation change or wording change."""
    # 1. Must involve at least one hyphen/dash
    if not (_HYPHEN_RE.search(approved) or _HYPHEN_RE.search(suspect)):
        return False
    # 2. Exclude horizontal divider lines / markdown rules (3 or more consecutive hyphens)
    if _REPEATED_HYPHENS_RE.search(approved) or _REPEATED_HYPHENS_RE.search(suspect):
        return False

    # 3. Alphanumeric content MUST be identical (ignoring case, whitespace, and punctuation)
    alnum_a = _alnum_only(approved).casefold()
    alnum_b = _alnum_only(suspect).casefold()
    if not alnum_a or alnum_a != alnum_b:
        return False

    # 4. Check that hyphen count or hyphen placement actually differs:
    cnt_a = len(_HYPHEN_RE.findall(approved))
    cnt_b = len(_HYPHEN_RE.findall(suspect))
    if cnt_a != cnt_b:
        return True

    # Same count of hyphens: check if positions or spacing around them differ
    # (e.g. 'anti - inflammatory' vs 'anti-inflammatory', or 'pre-eclampsia' vs 'preeclamp-sia')
    no_ws_a = re.sub(r"\s+", "", approved)
    no_ws_b = re.sub(r"\s+", "", suspect)
    if no_ws_a.casefold() != no_ws_b.casefold():
        return True

    if approved != suspect and re.search(
        r"\s+" + _HYPHEN_CHARS_CLASS + "|" + _HYPHEN_CHARS_CLASS + r"\s+",
        approved + suspect,
    ):
        return True

    return False


def _classify_severity(
    tag: str,
    approved_slice: list[ComparisonToken],
    suspect_slice: list[ComparisonToken],
) -> str:
    """Priority-ladder severity classification -- see module docstring.

    The digit rule (originally "digit anywhere => critical") has been
    refined for RTL-heavy documents: Arabic pharmacological/chemical
    text is dense with digits (lot numbers, concentrations, section
    references), so the old blanket rule amplified almost every RTL
    misalignment into "critical".  The new rule extracts the ordered
    sequence of digit characters from each side and only escalates when
    the ACTUAL NUMBERS differ -- same digits in a different surrounding
    context (e.g. bracket mirroring, reading-order jitter) are allowed
    to fall through to the lower-severity tiers.
    """
    if tag != "replace":
        active_slice = approved_slice if tag == "delete" else suspect_slice
        active_text = " ".join(t.text for t in active_slice)
        if not any(c.isalnum() for c in active_text):
            return "minor"  # pure punctuation insertion or deletion (e.g. ';', ')', ').')
        if _is_figure_artwork_text(active_text):
            return "minor"  # vector chart/diagram axis labels or atom symbols
        # Any word present on one side and missing on the other is
        # always worth a human look; there's no safe auto-downgrade
        # for a word that simply isn't there.
        return "critical"

    approved_text = " ".join(t.text for t in approved_slice)
    suspect_text = " ".join(t.text for t in suspect_slice)

    if _is_figure_artwork_text(approved_text) or _is_figure_artwork_text(suspect_text):
        return "minor"

    # Digit-value changes (dosages, strengths, counts, dates...) are
    # always critical.  But now we only trigger this when the digit
    # CONTENT actually differs, not merely because digits are present
    # on both sides -- RTL documents mix digits into nearly every token.
    app_digits = _extract_digit_runs(approved_text)
    sus_digits = _extract_digit_runs(suspect_text)
    if app_digits != sus_digits and (app_digits or sus_digits):
        return "critical"

    if approved_text.casefold() == suspect_text.casefold():
        return "major"  # case-only change, e.g. "ROPINIROLE" vs "ropinirole"

    if _is_hyphenation(approved_text, suspect_text):
        return "minor"

    app_stripped = re.sub(r"\s+", "", _strip_punct(approved_text)).casefold()
    sus_stripped = re.sub(r"\s+", "", _strip_punct(suspect_text)).casefold()
    if app_stripped == sus_stripped:
        return "minor"  # punctuation-only or spacing-and-punctuation change (e.g. '5, 25,' vs '5,25,')

    if _looks_like_typo(approved_text, suspect_text):
        return "typo"  # e.g. "especially" vs "especial y" / "halucinations"

    return "critical"  # genuine wording change


# Superscript/subscript digits are part of the number they belong to and
# keep their position: "2⁴" -> ("2⁴",), so "2⁴" vs "24" (or "H₂O" vs
# "H2O") is a numeric change.
_DIGIT_RUN_RE = re.compile(r"[\d" + SCRIPT_DIGITS + "]+")


# Difference categories for the results list, derived from the same
# facts the severity ladder uses. Kept here, next to that ladder, so the
# UI never re-implements the classification.
CATEGORY_LABELS = {
    "added": "Inserted",
    "removed": "Deleted on suspect",
    "number": "Number/dosage",
    "wording": "Wording",
    "case": "Capitalization",
    "punctuation": "Punctuation/spacing",
    "hyphenation": "Hyphenation",
    "spelling": "Spelling",
    "moved": "Moved",
}
CATEGORY_ORDER = tuple(CATEGORY_LABELS)


def category_of(item: DiffItem) -> str:
    """Which `CATEGORY_LABELS` bucket a discrepancy belongs to."""
    if item.kind in ("reordered", "reordered_section"):
        return "moved"
    if item.kind == "added_in_suspect":
        return "added"
    if item.kind == "removed_in_suspect":
        return "removed"
    approved, suspect = item.approved_text, item.suspect_text
    if _extract_digit_runs(approved) != _extract_digit_runs(suspect):
        return "number"
    if _is_hyphenation(approved, suspect):
        return "hyphenation"
    if approved.casefold() == suspect.casefold():
        return "case"
    if re.sub(r"\s+", "", _strip_punct(approved)).casefold() == re.sub(
        r"\s+", "", _strip_punct(suspect)
    ).casefold():
        return "punctuation"
    if item.severity == "typo":
        return "spelling"
    return "wording"


def _extract_digit_runs(text: str) -> tuple[str, ...]:
    """Extract ordered digit-only runs from ``text``.

    Returns a tuple of strings, each being a contiguous run of digit
    characters.  Non-digit characters are stripped.  Two texts with the
    same digit runs in the same order contain the same numeric content
    even if punctuation/brackets/letters around them differ.

    Examples:
        "5 mg"       -> ("5",)
        "10 mg"      -> ("10",)
        "(5.3)"      -> ("5", "3")
        "section 2"  -> ("2",)
        "no digits"  -> ()
        "2⁴"         -> ("2⁴",)
    """
    return tuple(_DIGIT_RUN_RE.findall(text))


def _contains_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def _strip_punct(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum() or ch.isspace())


def _looks_like_typo(approved_text: str, suspect_text: str) -> bool:
    """True if two sides look like the same word mangled by a dropped
    letter or a stray inserted space, rather than a real wording change.

    Deliberately conservative: comparison strips everything except
    letters (so trailing punctuation like the period in "halucinations."
    or the comma in "cals," never blocks a match, and a stray inserted
    space like "il ness" collapses back to "ilness"), and what's left
    must be purely alphabetic on both sides -- so this never fires on
    numbers or mixed alphanumeric content. See the digit check in
    `_classify_severity`, which always runs first and wins regardless
    of how "similar" a numeric change might look here.
    """
    approved_letters = "".join(ch for ch in approved_text if ch.isalpha()).casefold()
    suspect_letters = "".join(ch for ch in suspect_text if ch.isalpha()).casefold()

    if not approved_letters or not suspect_letters:
        return False
    if abs(len(approved_letters) - len(suspect_letters)) > _TYPO_MAX_LENGTH_DELTA:
        return False

    ratio = difflib.SequenceMatcher(None, approved_letters, suspect_letters).ratio()
    return ratio >= _TYPO_SIMILARITY_THRESHOLD

