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
    critical > major > minor > typo

Text that only MOVED is not a difference at all (a concise version
reorders text as a matter of course): `_pair_moved_text` pairs its words
one for one, so it is matched like any other text, and only an edit
inside it is reported -- see the note above that function. ("reorder"
stays in SEVERITY_ORDER for reviews saved by earlier versions; nothing
is rated so any more.)

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
   nothing, wherever the section now stands; sections present on only
   one side become a single removed/added item (never a cascade).
4. MOVED-TEXT PASS: section chunking depends on header detection, and
   a print layout can defeat it -- e.g. a figure's axis label read as a
   heading swallows the rest of a section into an unrelated chunk.
   Whatever the section pairs leave unexplained (removed runs, added
   runs, both sides of replacements, unmatched sections) is searched
   for runs of `_MIN_MOVED_RUN_WORDS`+ identical words present on both
   sides. Those are paired as moved text (matched, no row) and the
   remaining text is diffed again without them, so only genuine
   differences are reported. `_pair_moved_text` does the same, once
   more, over everything still unexplained at the end.

Documents without detected numbered headers (plain paragraphs, a
single header, or tables) fall back to the original whole-stream
word diff -- byte-for-byte the MVP-1..7 behavior.
"""

from __future__ import annotations

import bisect
import difflib
import functools
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.pdf_extractor import ComparisonToken, Word
from core.script_text import _SUBSCRIPT_MAP, _SUPERSCRIPT_MAP, SCRIPT_DIGITS
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
    # The words of each document this row stands for (None for a row not
    # built from the word streams). Used once the comparison is done, to
    # pair text that only moved (`_pair_moved_text`); never shown.
    approved_span: Optional[list] = field(default=None, repr=False, compare=False)
    suspect_span: Optional[list] = field(default=None, repr=False, compare=False)


# (approved token, suspect token) pairs the matcher found identical.
TokenMatches = list[tuple[ComparisonToken, ComparisonToken]]


def diff_documents(
    approved_tokens: list[ComparisonToken],
    suspect_tokens: list[ComparisonToken],
    matches: Optional[TokenMatches] = None,
    on_found: Optional[Callable[[list[DiffItem]], None]] = None,
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
    sections are matched globally and compared where they stand, not
    reported as removed/added cascades. Otherwise the whole-stream
    word diff runs unchanged. Either way, text that only moved is
    paired, not reported (`_pair_moved_text`).

    If ``matches`` is given, every pair of tokens found identical (and
    therefore corresponding to each other) is appended to it -- the UI
    uses this to take the other document to the same text.

    ``on_found`` is called with each batch of differences as the
    section it came from is compared, so a list can fill as the
    comparison runs instead of all at once at the end. The batches are
    PROVISIONAL: the passes that run once the whole document has been
    compared (pairing a deletion with the matching insertion elsewhere,
    demoting text that only moved, sorting by severity) can still
    change or merge them, so the returned list -- not the batches -- is
    the authoritative result.
    """
    def _found(items: list[DiffItem]) -> None:
        """Hand on the differences worth showing while the comparison
        runs: the spacing artifacts the closing pass drops anyway are
        left out here too, so the list grows instead of filling up and
        then shrinking."""
        if on_found is None:
            return
        worth_showing = [item for item in items if not _is_whitespace_artifact(item)]
        if worth_showing:
            on_found(worth_showing)

    if matches is None:
        matches = []  # moved text is paired through it (`_pair_moved_text`)
    _harmonize_line_break_hyphens(approved_tokens, suspect_tokens)
    sections_a = _split_sections(approved_tokens)
    sections_b = _split_sections(suspect_tokens)
    if sections_a is not None and sections_b is not None:
        return _diff_by_sections(
            approved_tokens, suspect_tokens, sections_a, sections_b, matches, on_found=_found
        )
    whole = _diff_runs(approved_tokens, suspect_tokens, matches)
    _found(whole)
    return _finalize_items(whole, approved_tokens, suspect_tokens, matches)


def _harmonize_line_break_hyphens(
    approved_tokens: list[ComparisonToken], suspect_tokens: list[ComparisonToken]
) -> None:
    """Decide, for each word stitched across a line break, whether the
    hyphen it was broken at belongs to the word.

    "placebo-" at the end of one line and "treated" on the next is read
    as "placebotreated" -- right for typesetter hyphenation
    ("pharmaco-" + "kinetics"), wrong for a compound that merely broke
    at its own hyphen. Neither Word nor a browser hyphenates on its own,
    so in a Word draft against an SPL nearly every such break is a real
    hyphen, and each one became a "hyphenation" difference ("placebo-
    treated" against "placebotreated") that is on neither page.

    The words printed WHOLE, on one line, settle it -- in either
    document: when the word appears there with the hyphen and never
    without, the hyphen is put back (the token then reads as printed).
    Otherwise -- written without, both ways, or never on one line --
    the stitched form stands, as before. Decided afresh on every call,
    from the stitched form: the same extracted document compared next
    with a different one gets that comparison's answer, not this one's.
    """
    whole = {
        _EDGE_PUNCTUATION.sub("", t.text)
        for tokens in (approved_tokens, suspect_tokens)
        for t in tokens
        if getattr(t, "line_break_hyphen", None) is None
    }
    # Two words printed one after the other: a dash between them at a line
    # end is not a word broken in two when the halves are words of their
    # own and the joined word is printed nowhere -- "INFORMATION-" over
    # "needed", a dash typed after a word that the line then broke at. Read
    # stitched ("INFORMATIONneeded"), it compared as a spacing slip against
    # "INFORMATION needed" and was dropped.
    word_pairs = {
        (_EDGE_PUNCTUATION.sub("", a.text), _EDGE_PUNCTUATION.sub("", b.text))
        for tokens in (approved_tokens, suspect_tokens)
        for a, b in zip(tokens, tokens[1:])
        if getattr(a, "line_break_hyphen", None) is None and getattr(b, "line_break_hyphen", None) is None
    }
    for tokens in (approved_tokens, suspect_tokens):
        for token in tokens:
            at = getattr(token, "line_break_hyphen", None)
            if at is None:
                continue
            stitched = _stitched_text(token)
            hyphenated = stitched[:at] + "-" + stitched[at:]
            # The word itself, without what clings to it: "short-term,"
            # is evidence for "short-" + "term." as much as "short-term".
            with_hyphen = _EDGE_PUNCTUATION.sub("", hyphenated) in whole
            without = _EDGE_PUNCTUATION.sub("", stitched) in whole
            apart = (
                _EDGE_PUNCTUATION.sub("", stitched[:at]),
                _EDGE_PUNCTUATION.sub("", stitched[at:]),
            ) in word_pairs
            token.text = hyphenated if (with_hyphen or apart) and not without else stitched


# Punctuation at either end of a word (and nothing else).
_EDGE_PUNCTUATION = re.compile(r"^[^\w]+|[^\w]+$")


def _stitched_text(token: ComparisonToken) -> str:
    """A token's text with a line-break hyphen left out (as stitched)."""
    at = getattr(token, "line_break_hyphen", None)
    if at is not None and token.text[at : at + 1] == "-" and len(token.text) > at + 1:
        return token.text[:at] + token.text[at + 1 :]
    return token.text


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
        if tag == "replace":
            split = _split_artwork(approved_tokens, suspect_tokens, i1, i2, j1, j2, matches, len(items))
            if split is not None:
                items.extend(split)
                continue
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
        # The same characters, only spaced differently ("(5.2)]." against
        # an SPL's "( 5.2) ].") is ONE spacing difference: re-diffing it
        # character by character split it into an inserted "(" and two
        # changed fragments.
        same_letters = tag == "replace" and "".join(t.text for t in approved_slice) == "".join(
            t.text for t in suspect_slice
        )
        if tag == "replace" and len(approved_slice) > 1 and len(suspect_slice) > 1 and not same_letters:
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


def _split_artwork(
    approved_tokens: list[ComparisonToken],
    suspect_tokens: list[ComparisonToken],
    i1: int, i2: int, j1: int, j2: int,
    matches: Optional[TokenMatches],
    base_index: int,
) -> Optional[list[DiffItem]]:
    """A replacement in which only ONE side holds an artwork sheet's box
    (or its note) -- an outsert against the Word draft it was set from --
    as rows: the box words deleted (or inserted) on their own, the rest
    compared as usual. Set against the draft's text, the box read as a
    "change" of "PRODUCT NAME" into "FDA-Approved Patient Labeling".
    None when neither side, or both, hold artwork."""
    a_art = [getattr(t, "artwork", False) for t in approved_tokens[i1:i2]]
    b_art = [getattr(t, "artwork", False) for t in suspect_tokens[j1:j2]]
    if any(a_art) == any(b_art):
        return None
    items: list[DiffItem] = []
    if any(a_art):
        rest = [t for t, art in zip(approved_tokens[i1:i2], a_art) if not art]
        for start, stop in _flag_runs(a_art):
            items.append(_make_diff_item(
                "delete", approved_tokens[i1 + start:i1 + stop], [], index=base_index + len(items),
                approved_tokens=approved_tokens, suspect_tokens=suspect_tokens,
                i1=i1 + start, i2=i1 + stop, j1=j1, j2=j1,
            ))
        others = suspect_tokens[j1:j2]
        items.extend(_items_from_opcodes(rest, others, _opcodes(rest, others), matches))
    else:
        rest = [t for t, art in zip(suspect_tokens[j1:j2], b_art) if not art]
        for start, stop in _flag_runs(b_art):
            items.append(_make_diff_item(
                "insert", [], suspect_tokens[j1 + start:j1 + stop], index=base_index + len(items),
                approved_tokens=approved_tokens, suspect_tokens=suspect_tokens,
                i1=i1, i2=i1, j1=j1 + start, j2=j1 + stop,
            ))
        others = approved_tokens[i1:i2]
        items.extend(_items_from_opcodes(others, rest, _opcodes(others, rest), matches))
    return items


def _flag_runs(flags: list[bool]) -> list[tuple[int, int]]:
    """(start, stop) of each run of True in ``flags``."""
    runs, start = [], None
    for k, flag in enumerate(flags + [False]):
        if flag and start is None:
            start = k
        elif not flag and start is not None:
            runs.append((start, k))
            start = None
    return runs


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
        approved_span=list(approved_slice),
        suspect_span=list(suspect_slice),
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


def _is_whitespace_artifact(item: DiffItem) -> bool:
    """Whether a difference is only a word drawn in pieces -- the same
    letters with a space somewhere inside a word ("spe cifically"), which
    is how extraction reports a word a PDF happens to draw in two runs.
    Dropped from the results.

    A space that appears or disappears NEXT TO A DIGIT is a real
    typesetting difference on a label -- "100 mg" against "100mg",
    "10 000" against "10000", "\u2265 5%" against "\u22655%" -- and is
    reported (as a minor spacing difference). Splits between letters and
    signs ("(N M S )", "Co- codamol") are how extraction reports
    letter-spaced or wrapped text, and are dropped.
    """
    if item.kind != "text_changed":
        return False
    approved, suspect = item.approved_text, item.suspect_text
    bare = re.sub(r"\s+", "", approved)
    if bare != re.sub(r"\s+", "", suspect):
        return False
    gaps_a, gaps_b = _space_positions(approved), _space_positions(suspect)
    for position in gaps_a ^ gaps_b:
        if 0 < position < len(bare) and (bare[position - 1].isdigit() or bare[position].isdigit()):
            return False
    return True


def _is_whitespace_only(approved: str, suspect: str) -> bool:
    """The same characters, spaced differently -- raised and lowered text
    compared as written either way: "α_{2C}" (one lowered run) and "α₂
    _{C}" (the same run split in two) are one subscript."""
    if approved == suspect:
        return False
    if re.sub(r"\s+", "", approved) == re.sub(r"\s+", "", suspect):
        return True
    return _script_canonical(approved) == _script_canonical(suspect)


# Each raised/lowered character -> (marker, the character it raises/lowers).
_UNSCRIPT = {
    **{raised: ("^", base) for base, raised in _SUPERSCRIPT_MAP.items()},
    **{lowered: ("_", base) for base, lowered in _SUBSCRIPT_MAP.items()},
}


def _script_canonical(text: str, *, plain: bool = False) -> str:
    """``text`` with every raised/lowered run written "^{...}" / "_{...}"
    (neighbouring runs of a kind joined) and no whitespace; ``plain``
    writes them as ordinary text instead."""
    out = []
    for ch in text:
        marker, base = _UNSCRIPT.get(ch, (None, ch))
        out.append(base if marker is None or plain else f"{marker}{{{base}}}")
    canon = re.sub(r"\s+", "", "".join(out))
    if plain:
        return re.sub(r"[_^]\{([^}]*)\}", r"\1", canon)
    previous = None
    while previous != canon:
        previous, canon = canon, re.sub(r"([_^])\{([^}]*)\}\1\{", r"\1{\2", canon)
    return canon


def _is_script_only(approved: str, suspect: str) -> bool:
    """The same letters, only raised or lowered differently -- "Cmax" /
    "Cₘₐₓ" -- with no digit among them: a lost subscript on a word is a
    formatting slip, while "2⁴" / "24" or "H₂O" / "H2O" change a value and
    are left to the number rules."""
    if approved == suspect or _script_canonical(approved, plain=True) != _script_canonical(suspect, plain=True):
        return False
    return not any(ch.isdigit() or ch in SCRIPT_DIGITS for ch in approved + suspect)


def _space_positions(text: str) -> set[int]:
    """Where the whitespace runs sit, counted in non-space characters:
    "ab c" -> {2}."""
    positions: set[int] = set()
    count = 0
    previous_space = False
    for ch in text.strip():
        if ch.isspace():
            if not previous_space:
                positions.add(count)
            previous_space = True
        else:
            count += 1
            previous_space = False
    return positions


def _finalize_items(
    items: list[DiffItem],
    approved_tokens: Optional[list[ComparisonToken]] = None,
    suspect_tokens: Optional[list[ComparisonToken]] = None,
    matches: Optional[TokenMatches] = None,
) -> list[DiffItem]:
    """Shared tail of every diff path: text that only moved is paired
    (`_pair_moved_text`), identical-text whitespace artifacts are
    filtered, then severity sort and stable index renumbering.

    Moved text is paired only for a whole comparison, which records the
    pairs in ``matches`` (`diff_documents`). A rematch passes none: the
    reviewer chose what to compare, and a pairing it could not keep would
    only turn rows into unplaced fragments."""
    if approved_tokens is not None and suspect_tokens is not None and matches is not None:
        items = _pair_moved_text(items, approved_tokens, suspect_tokens, matches)
    items = [item for item in items if not _is_whitespace_artifact(item)]
    _rate_systematic_letter_loss(items)
    items.sort(key=lambda item: SEVERITY_ORDER.index(item.severity))
    for new_index, item in enumerate(items):
        item.index = new_index
    return items


# Doubled-letter losses in longer words that show the text layer drops a
# letter pair (its ligature glyph) throughout the document.
_LETTER_LOSS_MIN_EVIDENCE = 3
# Short words a lost letter can turn one into another -- never noise.
_SHORT_WORDS = frozenset(
    "a an as at be by do go he if in is it me my no of on or so to up us we "
    "al am ad ed el em en er es ex"
    .split()
)


def _lost_double(approved: str, suspect: str) -> Optional[tuple[str, str]]:
    """(the letter, the side that lost it) when one side is the other with
    one letter of a doubled pair gone ("well" / "wel": ("l", "suspect")),
    else None."""
    if " " in approved.strip() or " " in suspect.strip():
        return None
    a, b = _letters(approved), _letters(suspect)
    if abs(len(a) - len(b)) != 1:
        return None
    longer, shorter, side = (a, b, "suspect") if len(a) > len(b) else (b, a, "approved")
    for i in range(len(longer) - 1):
        if longer[i] == longer[i + 1] and longer[:i] + longer[i + 1:] == shorter:
            return longer[i], side
    return None


def _rate_systematic_letter_loss(items: list[DiffItem]) -> None:
    """Rate a short word's lost doubled letter as the text-layer noise it
    is, when the same document drops the same doubled letter in longer
    words too. Words under six letters are left out of `_looks_like_typo`
    -- one letter is often their whole meaning -- so "well" / "wel" was a
    major misspelling while "hallucinations" / "halucinations", the same
    "ll" glyph lost on the same page, was noise. With three or more such
    losses on the same side (the evidence), a short word losing that letter
    is noise too -- never when what is left is a word of its own ("to",
    "of")."""
    evidence: Counter = Counter()
    for item in items:
        if item.severity == "typo":
            lost = _lost_double(item.approved_text, item.suspect_text)
            if lost is not None:
                evidence[lost] += 1
    if not evidence:
        return
    for item in items:
        if item.kind != "text_changed" or item.severity != "major":
            continue
        lost = _lost_double(item.approved_text, item.suspect_text)
        if lost is None or evidence[lost] < _LETTER_LOSS_MIN_EVIDENCE:
            continue
        a, b = _letters(item.approved_text), _letters(item.suspect_text)
        if min((a, b), key=len) in _SHORT_WORDS:
            continue
        item.severity = "typo"


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


def _diff_by_sections(
    approved_tokens: list[ComparisonToken],
    suspect_tokens: list[ComparisonToken],
    sections_a: list[_Section],
    sections_b: list[_Section],
    matches: Optional[TokenMatches] = None,
    on_found: Optional[Callable[[list[DiffItem]], None]] = None,
) -> list[DiffItem]:
    """Per-section comparison for documents with detected headers.

    1. Match each suspect section globally against every approved
       section (position-agnostic).
    2. Diff matched pairs in isolation.
    3. Pair text the section pairing left unexplained but that exists
       on both sides (see the module note, step 5) and diff the rest
       again without it -- genuine internal edits are caught at their
       real severity; identical content produces nothing.
    4. A section or passage that only moved is not a difference: its
       words are matched where they are, and only edits inside it are
       reported (a concise version reorders text as a matter of course).
    5. Section text present on only one side becomes removed/added
       items -- never a cascade.
    """
    b_to_a, unmatched_a, unmatched_b = _match_sections(sections_a, sections_b)
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
    passages = _moved_passages(
        _find_moved_blocks(leftover_a, leftover_b, (approved_tokens, suspect_tokens)), leftover_a, leftover_b
    )
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
        found = _items_from_opcodes(tokens_a, tokens_b, opcodes, matches)
        items.extend(found)
        if on_found is not None and found:
            on_found(found)

    before_unmatched = len(items)
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
                    approved_span=list(run),
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
                    suspect_span=list(run),
                )
            )

    if on_found is not None and len(items) > before_unmatched:
        on_found(items[before_unmatched:])

    before_passages = len(items)
    for span_a, span_b in passages:
        # The moved text itself is matched (its words are paired); only
        # the edits inside it are rows -- moving is not a difference.
        items.extend(_diff_runs(span_a, span_b, matches))

    if on_found is not None and len(items) > before_passages:
        on_found(items[before_passages:])
    return _finalize_items(items, approved_tokens, suspect_tokens, matches)


# A moved passage must be at least this many identical words: shorter
# runs ("of the patient") recur everywhere and would pair unrelated text.
_MIN_MOVED_RUN_WORDS = 6
# Candidate positions examined per phrase (repeated boilerplate).
_MAX_MOVE_CANDIDATES = 25
# A moved block made only of phrases that recur in the documents ("[see
# Boxed Warning and Warnings and Precautions") says nothing about where
# its text went unless it is this long -- or stays within this share of
# the documents' length of where it was.
_MIN_BOILERPLATE_MOVE_WORDS = 12
_MAX_BOILERPLATE_DRIFT = 0.1


def _find_moved_blocks(
    runs_a: list[list[ComparisonToken]],
    runs_b: list[list[ComparisonToken]],
    whole: Optional[tuple[list[ComparisonToken], list[ComparisonToken]]] = None,
) -> list[tuple[list[ComparisonToken], list[ComparisonToken]]]:
    """Maximal runs of identical words present in both ``runs_a`` and
    ``runs_b`` (each a list of contiguous token runs), at least
    `_MIN_MOVED_RUN_WORDS` long. Order-independent, so passages that
    swapped places are found too; each token is used at most once.
    Longest matches are taken first, across the whole document: a phrase
    occurring at several places goes to the copy it matches longest. Taken
    in document order instead, "physical condition. It is not known if
    sildenafil tablets are safe" took the copy in "pregnant. It is not known
    if sildenafil tablets will harm your unborn baby" for its first seven
    words, and that sentence -- present in both documents -- was reported
    as deleted.

    With ``whole`` (the two documents' full token lists), a block made only
    of phrases that recur in them must be `_MIN_BOILERPLATE_MOVE_WORDS`
    long or stay near where it was: a 2017 sentence on page 22, deleted in
    the 2024 label, had its cross-reference "[see Boxed Warning and
    Warnings and Precautions" paired with the same words closing a
    different sentence on page 6."""
    k = _MIN_MOVED_RUN_WORDS
    boilerplate = _boilerplate_check(whole, k)
    texts_a = [[t.text for t in run] for run in runs_a]
    texts_b = [[t.text for t in run] for run in runs_b]
    index: dict[tuple[str, ...], list[tuple[int, int]]] = {}
    for r, texts in enumerate(texts_b):
        for p in range(len(texts) - k + 1):
            index.setdefault(tuple(texts[p : p + k]), []).append((r, p))

    # Every maximal common run (it can't be extended to the left).
    candidates: list[tuple[int, int, int, int, int]] = []  # (length, run_a, p, run_b, q)
    for ra, ta in enumerate(texts_a):
        for p in range(len(ta) - k + 1):
            for rb, q in index.get(tuple(ta[p : p + k]), ())[:_MAX_MOVE_CANDIDATES]:
                tb = texts_b[rb]
                if p > 0 and q > 0 and ta[p - 1] == tb[q - 1]:
                    continue
                length = k
                while p + length < len(ta) and q + length < len(tb) and ta[p + length] == tb[q + length]:
                    length += 1
                candidates.append((length, ra, p, rb, q))
    candidates.sort(key=lambda c: (-c[0], c[1], c[2], c[3], c[4]))

    used_a: set[tuple[int, int]] = set()
    used_b: set[tuple[int, int]] = set()
    blocks = []
    for length, ra, p, rb, q in candidates:
        # What is still free of it, on both sides, in stretches of k+.
        i = 0
        while i < length:
            if (ra, p + i) in used_a or (rb, q + i) in used_b:
                i += 1
                continue
            j = i
            while j < length and (ra, p + j) not in used_a and (rb, q + j) not in used_b:
                j += 1
            if j - i >= k and not boilerplate(runs_a[ra][p + i : p + j], runs_b[rb][q + i : q + j]):
                used_a.update((ra, p + n) for n in range(i, j))
                used_b.update((rb, q + n) for n in range(i, j))
                blocks.append((runs_a[ra][p + i : p + j], runs_b[rb][q + i : q + j]))
            i = j
    return blocks


def _boilerplate_check(
    whole: Optional[tuple[list[ComparisonToken], list[ComparisonToken]]], k: int
) -> Callable[[list[ComparisonToken], list[ComparisonToken]], bool]:
    """A test telling whether a candidate moved block (its words on each
    side) is boilerplate that moved too far to be trusted -- see
    `_find_moved_blocks`. Always False without the whole documents."""
    if whole is None:
        return lambda block_a, block_b: False
    tokens_a, tokens_b = whole
    texts_a, texts_b = [t.text for t in tokens_a], [t.text for t in tokens_b]
    recurring = {
        gram
        for texts in (texts_a, texts_b)
        for gram, count in Counter(tuple(texts[i : i + k]) for i in range(len(texts) - k + 1)).items()
        if count >= 2
    }
    position_a = {id(t): i / max(1, len(tokens_a)) for i, t in enumerate(tokens_a)}
    position_b = {id(t): i / max(1, len(tokens_b)) for i, t in enumerate(tokens_b)}

    def too_far(block_a: list[ComparisonToken], block_b: list[ComparisonToken]) -> bool:
        if len(block_a) >= _MIN_BOILERPLATE_MOVE_WORDS:
            return False
        words = [t.text for t in block_a]
        if not all(tuple(words[i : i + k]) in recurring for i in range(len(words) - k + 1)):
            return False
        drift = abs(position_a.get(id(block_a[0]), 0.0) - position_b.get(id(block_b[0]), 0.0))
        return drift > _MAX_BOILERPLATE_DRIFT

    return too_far


# Moved fragments at most this many words apart (on both sides) are one
# moved passage with small edits inside -- reported as one row; the
# edits themselves still get their own rows.
_MAX_MOVED_FRAGMENT_GAP = 5
# Farther apart, they are still one passage when the words between them
# are mostly the same words, ignoring case and punctuation: "Sildenafil
# tablets? • Take or give Sildenafil" against "sildenafil tablets? • Take
# or give sildenafil" (7 words) between two moved fragments of a Patient
# Information leaflet was left uncompared -- every word of it a critical
# "Inserted" row instead of two capitalization changes.
_MIN_MOVED_GAP_SIMILARITY = 0.5


def _gaps_alike(gap_a: list[ComparisonToken], gap_b: list[ComparisonToken]) -> bool:
    keys_a = [_alnum_key(t.text) for t in gap_a]
    keys_b = [_alnum_key(t.text) for t in gap_b]
    return difflib.SequenceMatcher(None, keys_a, keys_b, autojunk=False).ratio() >= _MIN_MOVED_GAP_SIMILARITY


def _moved_passages(
    blocks: list[tuple[list[ComparisonToken], list[ComparisonToken]]],
    runs_a: list[list[ComparisonToken]],
    runs_b: list[list[ComparisonToken]],
) -> list[tuple[list[ComparisonToken], list[ComparisonToken]]]:
    """Join moved blocks that follow each other in BOTH documents (same
    run; gap <= `_MAX_MOVED_FRAGMENT_GAP` words, or gaps that are mostly
    the same words) into passages: the contiguous span on each side from
    the first block's start to the last block's end, gaps (the edits)
    included."""
    position_a = {id(t): (r, i) for r, run in enumerate(runs_a) for i, t in enumerate(run)}
    position_b = {id(t): (r, i) for r, run in enumerate(runs_b) for i, t in enumerate(run)}

    def span(block, position):
        run, start = position[id(block[0])]
        return run, start, start + len(block)

    ordered = sorted(blocks, key=lambda blk: position_a[id(blk[0][0])])
    starts_b = [position_b[id(block_b[0])] for _, block_b in blocks]
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
            and 0 <= start_a - last[2]
            and 0 <= start_b - last[5]
            # Never across a block moved somewhere else: its words would
            # be paired twice.
            and not any(r == run_b and last[5] <= q < start_b for r, q in starts_b)
            and (
                max(start_a - last[2], start_b - last[5]) <= _MAX_MOVED_FRAGMENT_GAP
                or _gaps_alike(runs_a[run_a][last[2]:start_a], runs_b[run_b][last[5]:start_b])
            )
        ):
            last[2], last[5] = end_a, end_b
        else:
            spans.append([run_a, start_a, end_a, run_b, start_b, end_b])

    # The words before a passage's first fragment (after its last), up to
    # the edge of the unmatched text on both sides, belong to it too when
    # they are mostly the same words: "Know the medicines you or your
    # child" / "Know the medicines you" opening a moved paragraph.
    def alone(s, k_run, k_start, before):
        return not any(
            o is not s and o[k_run] == s[k_run] and (o[k_start] < s[k_start]) == before
            for o in spans
        )

    for s in spans:
        ra, sa, ea, rb, sb, eb = s
        if alone(s, 0, 1, True) and alone(s, 3, 4, True) and _gaps_alike(runs_a[ra][:sa], runs_b[rb][:sb]):
            s[1], s[4] = 0, 0
        if (alone(s, 0, 1, False) and alone(s, 3, 4, False)
                and _gaps_alike(runs_a[ra][ea:], runs_b[rb][eb:])):
            s[2], s[5] = len(runs_a[ra]), len(runs_b[rb])
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


# --- Moved text (a concise version reorders as a matter of course) -----
#
# Text that is in both documents, only somewhere else, is not a
# difference. It is MATCHED -- its words paired one for one, so it is shown
# like any matched text -- and only an edit inside it is reported. Never by
# text alone: a word is paired only with a word no other match has taken,
# so a sentence printed twice in one document and once in the other leaves
# its second copy a deletion.
#
# 1. A deleted row and an inserted row of the same words: paired, one for
#    one. A row of one or two words only when the pair cannot be chance --
#    a word of 4+ letters, and as many such rows deleted as inserted
#    ("Total cholesterol" moved across a table) -- and then as
#    a `TrustedPair`, shown matched though no text around it confirms it.
#    A lone "and" or "All" deleted here and inserted there stays two rows.
#    Three or more words with the same letters in another case or
#    punctuation ("KEEP OUT OF REACH" / "Keep out of reach"): one row
#    comparing the two.
# 2. A passage of `_MIN_MOVED_RUN_WORDS`+ identical words among what is
#    still unmatched: compared as a unit -- its identical words paired, the
#    edits inside it rows of their own.
# 3. A deleted or inserted row of 3+ words found whole, unmatched, in the
#    other document: paired. Only a WHOLE row: a short phrase inside a
#    different sentence ("of the patient") recurs by chance.
# Re-cased whole rows and phrases found inside other rows need at least
# this many words: at one or two, "by" / "By" or "of the" recur by chance.
_MIN_MOVED_ROW_WORDS = 3
# A word this long (letters) makes a short moved row distinctive.
_MIN_DISTINCTIVE_LETTERS = 4


class TrustedPair(tuple):
    """A matched (approved, suspect) token pair confirmed by what it is
    rather than by matched text around it: a word of a short row found
    moved, whole, to a place of its own (see `_pair_moved_text`).
    `core.token_map.TokenMap` shows it matched."""


def _distinctive_short_row(text: str, deleted: Counter, inserted: Counter) -> bool:
    """A one- or two-word row that cannot be paired by chance: it holds a
    word of `_MIN_DISTINCTIVE_LETTERS`+ letters, and exactly as many rows
    reading so are deleted as are inserted (each copy moved: paired in
    order)."""
    return (
        any(sum(ch.isalpha() for ch in word) >= _MIN_DISTINCTIVE_LETTERS for word in text.split())
        and deleted[text] == inserted[text]
    )


def _pair_moved_text(
    items: list[DiffItem],
    approved_tokens: list[ComparisonToken],
    suspect_tokens: list[ComparisonToken],
    matches: Optional[TokenMatches],
) -> list[DiffItem]:
    """``items`` with the text that only moved paired (into ``matches``)
    instead of reported, and every row it touched rebuilt from the words
    it still stands for."""
    if matches is None:
        matches = []
    position_a = {id(t): i for i, t in enumerate(approved_tokens)}
    position_b = {id(t): j for j, t in enumerate(suspect_tokens)}
    taken_a: set[int] = set()
    taken_b: set[int] = set()
    extra: list[DiffItem] = []

    def free(span: Optional[list], taken: set[int]) -> bool:
        return bool(span) and not any(id(t) in taken for t in span)

    def pair(span_a: list, span_b: list, trusted: bool = False) -> None:
        matches.extend(TrustedPair(p) if trusted else p for p in zip(span_a, span_b))
        taken_a.update(id(t) for t in span_a)
        taken_b.update(id(t) for t in span_b)

    removed = [it for it in items if it.kind == "removed_in_suspect" and it.approved_span]
    added = [it for it in items if it.kind == "added_in_suspect" and it.suspect_span]
    deleted_texts = Counter(it.approved_text for it in removed)
    inserted_texts = Counter(it.suspect_text for it in added)

    # 1. Whole rows: the same words, or the same letters re-cased/re-punctuated.
    exact: dict[str, list[DiffItem]] = defaultdict(list)
    loose: dict[str, list[DiffItem]] = defaultdict(list)
    for it in added:
        exact[it.suspect_text].append(it)
        if len(it.suspect_span) >= _MIN_MOVED_ROW_WORDS:
            loose[_alnum_key(it.suspect_text)].append(it)
    for it in removed:
        # A row of bare marks ("•") says nothing about where it belongs:
        # one inserted before "If you take too much" was paired with a
        # bullet 40 pages away.
        twin = next(
            (o for o in exact.get(it.approved_text, ())
             if free(o.suspect_span, taken_b) and len(o.suspect_span) == len(it.approved_span)),
            None,
        ) if _alnum_key(it.approved_text) else None
        if twin is not None and len(it.approved_span) < _MIN_MOVED_ROW_WORDS:
            if _distinctive_short_row(it.approved_text, deleted_texts, inserted_texts):
                pair(it.approved_span, twin.suspect_span, trusted=True)
            continue
        if twin is not None:
            pair(it.approved_span, twin.suspect_span)
            continue
        if len(it.approved_span) < _MIN_MOVED_ROW_WORDS:
            continue
        twin = next((o for o in loose.get(_alnum_key(it.approved_text), ()) if free(o.suspect_span, taken_b)), None)
        if twin is not None and _alnum_key(it.approved_text):
            # Moved AND changed: one row setting the two side by side.
            taken_a.update(id(t) for t in it.approved_span)
            taken_b.update(id(t) for t in twin.suspect_span)
            extra.append(_rebuilt_row("text_changed", it.approved_span, twin.suspect_span))

    # 2. Passages of identical words among what is still unmatched.
    runs_a = _unmatched_runs(items, "approved", taken_a, position_a)
    runs_b = _unmatched_runs(items, "suspect", taken_b, position_b)
    found = _find_moved_blocks(runs_a, runs_b, (approved_tokens, suspect_tokens))
    for span_a, span_b in _moved_passages(found, runs_a, runs_b):
        taken_a.update(id(t) for t in span_a)
        taken_b.update(id(t) for t in span_b)
        extra.extend(_diff_runs(span_a, span_b, matches))

    # 3. Whole short rows found, unmatched, in the other document.
    for rows, own_taken, other_runs, other_taken, side in (
        (removed, taken_a, _unmatched_runs(items, "suspect", taken_b, position_b), taken_b, "approved"),
        (added, taken_b, _unmatched_runs(items, "approved", taken_a, position_a), taken_a, "suspect"),
    ):
        for it in rows:
            span = it.approved_span if side == "approved" else it.suspect_span
            if len(span) < _MIN_MOVED_ROW_WORDS or not free(span, own_taken):
                continue
            words = [t.text for t in span]
            found = _find_unmatched_run(other_runs, words, other_taken)
            if found is not None:
                if side == "approved":
                    pair(span, found)
                else:
                    pair(found, span)

    rebuilt: list[DiffItem] = []
    for it in items:
        rebuilt.extend(_without_paired(it, taken_a, taken_b, position_a, position_b))
    return rebuilt + extra


def _alnum_key(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum()).casefold()


def _unmatched_runs(
    items: list[DiffItem], side: str, taken: set[int], position: dict[int, int]
) -> list[list[ComparisonToken]]:
    """The words of one document still in a row and not paired, as runs
    of consecutive words (document order)."""
    words: dict[int, ComparisonToken] = {}
    for it in items:
        span = it.approved_span if side == "approved" else it.suspect_span
        for token in span or ():
            if id(token) not in taken and id(token) in position:
                words[position[id(token)]] = token
    runs: list[list[ComparisonToken]] = []
    previous = None
    for index in sorted(words):
        if previous is None or index != previous + 1:
            runs.append([])
        runs[-1].append(words[index])
        previous = index
    return runs


def _find_unmatched_run(
    runs: list[list[ComparisonToken]], words: list[str], taken: set[int]
) -> Optional[list[ComparisonToken]]:
    n = len(words)
    for run in runs:
        for start in range(len(run) - n + 1):
            piece = run[start:start + n]
            if [t.text for t in piece] == words and not any(id(t) in taken for t in piece):
                return piece
    return None


def _rebuilt_row(
    kind: str, span_a: list, span_b: list, approved_caret=None, suspect_caret=None
) -> DiffItem:
    tag = {"text_changed": "replace", "removed_in_suspect": "delete", "added_in_suspect": "insert"}[kind]
    return DiffItem(
        index=0,
        kind=kind,
        severity=_classify_severity(tag, span_a, span_b),
        approved_text=" ".join(t.text for t in span_a),
        suspect_text=" ".join(t.text for t in span_b),
        approved_location=_location_of(span_a),
        suspect_location=_location_of(span_b),
        approved_caret=approved_caret if not span_a else None,
        suspect_caret=suspect_caret if not span_b else None,
        approved_span=list(span_a),
        suspect_span=list(span_b),
    )


def _without_paired(
    item: DiffItem,
    taken_a: set[int],
    taken_b: set[int],
    position_a: dict[int, int],
    position_b: dict[int, int],
) -> list[DiffItem]:
    """``item`` as it stands once moved text was paired out of it: itself
    if untouched, nothing if all of it moved, else rows for what is left
    (a deletion/insertion split at the gaps the moved words left)."""
    span_a = item.approved_span or []
    span_b = item.suspect_span or []
    left_a = [t for t in span_a if id(t) not in taken_a]
    left_b = [t for t in span_b if id(t) not in taken_b]
    if len(left_a) == len(span_a) and len(left_b) == len(span_b):
        return [item]
    if not left_a and not left_b:
        return []
    if left_a and left_b:
        return [_rebuilt_row("text_changed", left_a, left_b)]
    if left_a:
        return [
            _rebuilt_row("removed_in_suspect", part, [], suspect_caret=item.suspect_caret)
            for part in _consecutive_parts(left_a, position_a)
        ]
    return [
        _rebuilt_row("added_in_suspect", [], part, approved_caret=item.approved_caret)
        for part in _consecutive_parts(left_b, position_b)
    ]


def _consecutive_parts(tokens: list, position: dict[int, int]) -> list[list]:
    parts: list[list] = []
    previous = None
    for token in tokens:
        index = position.get(id(token))
        if not parts or index is None or previous is None or index != previous + 1:
            parts.append([])
        parts[-1].append(token)
        previous = index
    return parts


_MAX_SLUG_WORDS = 12
_MIN_AXIS_NUMBER_SHARE = 0.6

# Chemical-structure atom labels. A token counts only when it is written
# the way a structure diagram writes it (capitalised, "Cl", "CH3", "OH"),
# never an English word that happens to use the same letters ("no", "of",
# "off", "soon").
_ATOM_TOKENS = {"H", "C", "N", "O", "F", "P", "S", "Cl", "Br", "CL", "BR", "HC", "CH",
                "H3C", "CH3", "OH", "NH", "NH2", "NN", "HO", "HN", "CN", "NC"}
_ATOM_FORMULA_RE = re.compile(r"^(?:[CHNOFPS]|Cl|Br)(?:[CHNOFPS]|Cl|Br|\d)*$")
_ATOM_SYMBOLS = {"H", "C", "N", "O", "F", "P", "S", "Cl", "Br"}
# English words that the formula pattern above would otherwise accept.
_ATOM_LOOKALIKE_WORDS = {"NO", "ON", "OF", "OFF", "SO", "SOON", "HOP", "SHOP", "CHOP",
                         "POSH", "SNOOP", "NOON", "HOOF", "PHONS", "FONS"}

# Words that only occur in plot/forest-plot labelling. One of these must be
# present before the chart-vocabulary rule applies at all: the softer words
# in `_CHART_CONTEXT_WORDS` ("severe", "rate", "drug", "change") are plain
# label prose and on their own say nothing about a chart.
_CHART_MARKER_WORDS = {"responders", "cmax", "auc", "pk", "ci", "cl", "fold", "postdialysis",
                       "ad-1", "ad-2", "tmax", "cmin"}
_CHART_CONTEXT_WORDS = _CHART_MARKER_WORDS | {
    "response", "rate", "percent", "weeks", "trial", "change", "normal", "severe", "moderate",
    "mild", "impairment", "reference", "population", "description", "interacting",
    "inhibitor", "drug", "and", "renal", "hepatic",
}
_DEVICE_CALLOUT_WORDS = {"adaptor", "plunger", "syringe", "bottle", "cap", "slow", "blister",
                         "vial", "needle", "pouch", "tip", "barrel"}


def _artwork_word(token: str) -> str:
    return token.lower().strip("()%:\u2020\u2021\u00a7*/!.,;")


def _is_axis_number(token: str) -> bool:
    return bool(re.match(r"^\d+(\.\d+)?%?$", token.rstrip(".,;:")))


def _is_figure_artwork_text(text: str) -> bool:
    """Whether ``text`` reads like the text inside a figure rather than
    label prose: plot axis ticks and forest-plot labels, structure-diagram
    atom symbols, device-diagram callouts, or a production slug.

    This only says what the text LOOKS like. Whether a difference in it
    may be demoted is decided by `_classify_severity`, which never lets
    it outrank a changed number and never demotes text that carries
    label content (`_carries_label_content`).
    """
    tokens = text.split()
    if not tokens:
        return False

    # 1. Structure-diagram atom labels: "O HC 3 N N F", "O O O N NN O H N S Cl O".
    #    Several tokens, every one an atom label or a bare digit, at least
    #    one a lone element symbol -- so no English word qualifies.
    cleaned = [t.strip(".,;:()") for t in tokens]
    cleaned = [t for t in cleaned if t]
    if len(cleaned) >= 3 and all(
        (t in _ATOM_TOKENS or t.isdecimal() or _ATOM_FORMULA_RE.match(t))
        and t.upper() not in _ATOM_LOOKALIKE_WORDS
        for t in cleaned
    ) and any(t in _ATOM_SYMBOLS for t in cleaned):
        return True

    # 2. Production slug with part numbers: "1202-2025-06 Rivaroxaban For Oral
    #    Suspension 2xxxxxx", "2xxxxxx".
    if len(tokens) <= _MAX_SLUG_WORDS and (
        any(re.search(r"\d{4,}-\d{4,}-\d+|\d{7}-\d{2}|\d+x{3,}", t, re.I) for t in tokens)
        or (len(tokens) == 1 and re.match(r"^\d+x+$", tokens[0], re.I))
    ):
        return True

    # 3. Axis tick runs / syringe graduations: mostly bare numbers. Units do
    #    not count towards the share -- "20 mg ... 5 ml" is a dose, not an axis.
    numbers = [t for t in tokens if _is_axis_number(t)]
    if len(numbers) >= 5 and len(numbers) >= len(tokens) * _MIN_AXIS_NUMBER_SHARE:
        return True
    if len(numbers) >= 8 and len(numbers) >= len(tokens) * 0.4:
        return True  # a graduation scale with its labels: "Side 1: ... Tip 0.5 1 1.5 ... 5 mL"

    # 4. Forest-plot / chart labelling: needs a word that only charts use,
    #    and then a high share of chart vocabulary overall. Words are
    #    matched whole -- "rated" is not "rate".
    words = [_artwork_word(t) for t in tokens]
    if len(tokens) >= 4 and any(w in _CHART_MARKER_WORDS for w in words):
        chart = sum(1 for w, t in zip(words, tokens) if w in _CHART_CONTEXT_WORDS or _is_axis_number(t))
        if chart / len(tokens) >= 0.5:
            return True

    # 5. Device / pack diagram callouts: a few words, all of them part names
    #    ("SLOW!", "bottle adaptor"), or several part names among a short
    #    run ("Cap Bottle adaptor Do not remove the bottle adaptor").
    callouts = [w for w in words if w in _DEVICE_CALLOUT_WORDS]
    if callouts and len(tokens) <= 3 and len(callouts) == len(tokens):
        return True
    if len(callouts) >= 2 and len(tokens) <= 10 and len(callouts) / len(tokens) >= 0.25:
        return True
    return False


# Text that must never be demoted as "artwork", however much it looks like
# a figure: a dose or strength, a negation or instruction, a storage or
# batch/expiry statement. Losing one of these from a label is a recall,
# not a cosmetic difference.
_DOSE_RE = re.compile(
    r"\d(?:[\d.,]*\d)?\s*(?:mg|mcg|\u00b5g|\u03bcg|ug|g|kg|ml|mL|l|L|iu|IU|units?|mmol|meq|mEq|%)(?![A-Za-z])",
)
_LABEL_CONTENT_WORDS = {
    "not", "no", "never", "avoid", "do", "don't", "must", "should", "warning", "caution",
    "keep", "discard", "store", "storage", "children", "child", "pregnancy", "pregnant",
    "breastfeeding", "contraindicated", "contraindications", "overdose", "fatal", "death",
    "allergy", "allergic", "batch", "lot", "exp", "expiry", "expires", "mfg", "mfd",
    "daily", "twice", "once", "dose", "dosage", "tablet", "tablets", "capsule", "capsules",
    "inject", "injection", "dilute", "shake", "remove", "only", "maximum", "exceed",
}


def _carries_label_content(text: str) -> bool:
    """Whether ``text`` says something a label is answerable for -- a
    dose, an instruction, a warning, a batch or expiry -- so that losing
    or changing it can never be written off as figure artwork."""
    tokens = text.split()
    words = {_artwork_word(t) for t in tokens}
    chart = bool(words & _CHART_MARKER_WORDS)
    doses = [m.group(0) for m in _DOSE_RE.finditer(text)]
    if chart:
        doses = [d for d in doses if not d.endswith("%")]  # "90% CI" is a chart label
    if doses:
        # A graduation scale ("0.5 1 1.5 2 ... 5 mL") ends in a unit too;
        # it is a scale, not a dose, when it is mostly a run of numbers.
        numbers = sum(1 for t in tokens if _is_axis_number(t))
        if not (numbers >= 8 and numbers >= len(tokens) * 0.4):
            return True
    return any(_artwork_word(t) in _LABEL_CONTENT_WORDS for t in tokens)


_HYPHEN_CHARS_CLASS = r"[\-\u2010\u2011\u2012\u2013\u2014\u2212\u00ad]"
_HYPHEN_RE = re.compile(_HYPHEN_CHARS_CLASS)
_REPEATED_HYPHENS_RE = re.compile(_HYPHEN_CHARS_CLASS + r"{3,}")


def _alnum_only(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


_EDGE_DASHES_RE = re.compile(r"^(" + _HYPHEN_CHARS_CLASS + r"*)(.*?)(" + _HYPHEN_CHARS_CLASS + r"*)$")


def _edge_dashes(text: str) -> list[list[str]]:
    """[leading dashes, word, trailing dashes] per word of ``text``; a
    word of dashes only ("OF ------") counts as trailing dashes of the
    word before it."""
    words: list[list[str]] = []
    for raw in text.split():
        lead, core, trail = _EDGE_DASHES_RE.match(raw).groups()
        if not core:
            if words:
                words[-1][2] += lead + trail
            else:
                words.append([lead + trail, "", ""])
            continue
        words.append([lead, core, trail])
    return words


def _edge_dash_change(approved: str, suspect: str) -> Optional[str]:
    """The severity of a difference that is only dashes added to or taken
    off the start or end of words -- "2011" / "2011-", "OF" / "OF------",
    "use" / "use--" -- or None when it is something else. Not hyphenation
    (a word broken or joined differently, "HIGHLIGHTS" / "HIGH-LIGHTS",
    "two-year" / "two year"): a dash typed before or after a word is a
    punctuation change, major; a sign put in front of a number ("1.4" /
    "-1.4") changes its value, critical."""
    a, b = _edge_dashes(approved), _edge_dashes(suspect)
    if approved == suspect or [w[1] for w in a] != [w[1] for w in b]:
        return None
    if all(x == y for x, y in zip(a, b)):
        return None  # the same dashes, spaced differently
    for (lead_a, core, _), (lead_b, _, _) in zip(a, b):
        if lead_a != lead_b and core[:1].isdigit():
            return "critical"
    return "major"


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
    # (e.g. 'anti - inflammatory' vs 'anti-inflammatory', or 'pre-eclampsia' vs 'preeclamp-sia').
    # Letters and dashes only: "AUC_{0–∞}" / "AUC_{0–∞.}" differ by a full
    # stop, not by where a dash stands.
    def letters_and_dashes(text: str) -> str:
        return "".join(ch for ch in text if ch.isalnum() or _HYPHEN_RE.match(ch)).casefold()

    if letters_and_dashes(approved) != letters_and_dashes(suspect):
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
            if _REPEATED_HYPHENS_RE.fullmatch(active_text.replace(" ", "")):
                # A run of dashes outside a ruled heading (those are left out
                # of the comparison, core/pdf_extractor.py): typed into the
                # text, as "OF ------" -- the same weight as "OF------".
                return "major"
            return "minor"  # pure punctuation insertion or deletion (e.g. ';', ')', ').')
        if _is_figure_artwork_text(active_text) and not _carries_label_content(active_text):
            return "minor"  # chart/diagram labels or atom symbols with no label content
        # Any word present on one side and missing on the other is
        # always worth a human look; there's no safe auto-downgrade
        # for a word that simply isn't there.
        return "critical"

    approved_text = " ".join(t.text for t in approved_slice)
    suspect_text = " ".join(t.text for t in suspect_slice)

    # The same characters with the spacing moved: never a changed fact
    # ("100 mg" vs "100mg", "9 8 5" vs "985"), reported as spacing.
    if _is_whitespace_only(approved_text, suspect_text):
        return "minor"
    if _is_script_only(approved_text, suspect_text):
        return "minor"

    # The same number grouped differently ("2633" vs "2,633"): the value
    # is unchanged, so it is a formatting difference, not a changed fact.
    # A decimal sign ("5.000" vs "5,000") or a digit change stays critical.
    if _is_thousands_grouping_only(approved_text, suspect_text):
        return "minor"
    # One list mark drawn with another glyph (Greek "ο" vs "○"): no word changed.
    if _is_list_mark_only(approved_text, suspect_text):
        return "minor"

    # Digit-value changes (dosages, strengths, counts, dates...) are
    # always critical, and this runs FIRST of the content rules: nothing below -- the artwork
    # rule included -- may outrank a changed number. Only triggers when
    # the digit CONTENT differs, not merely because digits are present on
    # both sides -- RTL documents mix digits into nearly every token.
    app_digits = _extract_digit_runs(approved_text)
    sus_digits = _extract_digit_runs(suspect_text)
    if app_digits != sus_digits and (app_digits or sus_digits):
        return "critical"
    # The same digits with a different decimal/thousands separator
    # ("5.000" vs "5,000") is a different number in another locale.
    if _number_forms(approved_text) != _number_forms(suspect_text):
        return "critical"
    # Dashes added to or taken off a word ("2011-", "OF------").
    edge_dash = _edge_dash_change(approved_text, suspect_text)
    if edge_dash is not None:
        return edge_dash

    # Figure text on BOTH sides (a chart axis read slightly differently).
    # One artwork-looking side is not enough: a short label paired with a
    # whole missing passage must not drag that passage down with it.
    if (
        _is_figure_artwork_text(approved_text)
        and _is_figure_artwork_text(suspect_text)
        and not _carries_label_content(approved_text)
        and not _carries_label_content(suspect_text)
    ):
        return "minor"

    if approved_text.casefold() == suspect_text.casefold():
        return "major"  # case-only change, e.g. "ROPINIROLE" vs "ropinirole"

    if _is_hyphenation(approved_text, suspect_text):
        return "minor"

    app_stripped = re.sub(r"\s+", "", _strip_punct(approved_text)).casefold()
    sus_stripped = re.sub(r"\s+", "", _strip_punct(suspect_text)).casefold()
    if app_stripped == sus_stripped:
        return "minor"  # punctuation-only or spacing-and-punctuation change (e.g. '5, 25,' vs '5,25,')

    if _changes_meaning(approved_text, suspect_text):
        return "critical"  # "hypertension"/"hypotension", "should"/"shouldn't"

    if _looks_like_typo(approved_text, suspect_text):
        return "typo"  # extraction noise: "especially" vs "especial y" / "halucinations"

    if _looks_like_misspelling(approved_text, suspect_text):
        return "major"  # the same word spelled differently: "contraindicated"/"contrainsdicated"

    return "critical"  # genuine wording change


# Superscript/subscript digits are part of the number they belong to and
# keep their position: "2⁴" -> ("2⁴",), so "2⁴" vs "24" (or "H₂O" vs
# "H2O") is a numeric change.
_DIGIT_RUN_RE = re.compile(r"[\d" + SCRIPT_DIGITS + "]+")
def _number_forms(text: str) -> tuple[str, ...]:
    """The separator written between each pair of neighbouring digit
    groups -- "." in "1.5", "," in "1,5", "" across a word boundary -- so
    a decimal point turned into a comma ("5.000" vs "5,000", 5 against
    5000 in another locale) is visible even though the digits are equal.
    Whitespace is ignored here: a space next to a number is a spacing
    difference, reported as such (see `_is_whitespace_only`)."""
    runs = list(_DIGIT_RUN_RE.finditer(text))
    forms = []
    for left, right in zip(runs, runs[1:]):
        between = re.sub(r"\s+", "", text[left.end():right.start()])
        forms.append(between if len(between) == 1 and not between.isalnum() else "")
    return tuple(forms)


_GROUPED_NUMBER_RE = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?![\d]|[.,]\d)")


def _ungroup_thousands(text: str) -> str:
    return re.sub(r"\s+", "", _GROUPED_NUMBER_RE.sub(lambda m: m.group().replace(",", ""), text))


def _is_thousands_grouping_only(approved: str, suspect: str) -> bool:
    """Whether the texts differ only in thousands separators."""
    return approved != suspect and _ungroup_thousands(approved) == _ungroup_thousands(suspect)


# Glyphs a layout uses as list marks; "o" and Greek "ο" only standing alone.
_LIST_MARK_GLYPHS = frozenset(
    ["•", "◦", "○", "ο", "o", "●", "▪", "■", "▫", "□",
     "➢", "►", "‣", "⁃", "✓", "➤", "❖", "◆", "◇",
     "", "", "", "", ""]
)


def _list_marks_folded(text: str) -> str:
    out: list[str] = []
    for word in text.split():
        word = "•" if word in _LIST_MARK_GLYPHS else word
        if not (word == "•" and out and out[-1] == "•"):
            out.append(word)
    return " ".join(out)


def _is_list_mark_only(approved: str, suspect: str) -> bool:
    """Whether the texts differ only in which glyph draws a list mark
    (both sides have one): "ο" vs "○", "o" vs "• •"."""
    return (
        approved != suspect
        and any(w in _LIST_MARK_GLYPHS for w in approved.split())
        and any(w in _LIST_MARK_GLYPHS for w in suspect.split())
        and _list_marks_folded(approved) == _list_marks_folded(suspect)
    )


# Difference categories for the results list, derived from the same
# facts the severity ladder uses. Kept here, next to that ladder, so the
# UI never re-implements the classification.
CATEGORY_LABELS = {
    "added": "Inserted",
    "removed": "Deleted on suspect",
    "number": "Number/dosage",
    "wording": "Wording",
    "case": "Capitalization",
    "punctuation": "Punctuation",
    "spacing": "Spacing",
    "hyphenation": "Hyphenation",
    "spelling": "Spelling",
    "script": "Sub/superscript",
}
CATEGORY_ORDER = tuple(CATEGORY_LABELS)


def category_of(item: DiffItem) -> str:
    """Which `CATEGORY_LABELS` bucket a discrepancy belongs to."""
    if item.kind == "added_in_suspect":
        return "added"
    if item.kind == "removed_in_suspect":
        return "removed"
    approved, suspect = item.approved_text, item.suspect_text
    if _is_whitespace_only(approved, suspect):
        return "spacing"
    if _is_script_only(approved, suspect):
        return "script"
    if _is_list_mark_only(approved, suspect):
        return "punctuation"
    if _extract_digit_runs(approved) != _extract_digit_runs(suspect) or _number_forms(
        approved
    ) != _number_forms(suspect):
        return "number"
    if _edge_dash_change(approved, suspect) is not None:
        return "punctuation"
    if _is_hyphenation(approved, suspect):
        return "hyphenation"
    if approved.casefold() == suspect.casefold():
        return "case"
    if re.sub(r"\s+", "", _strip_punct(approved)).casefold() == re.sub(
        r"\s+", "", _strip_punct(suspect)
    ).casefold():
        return "punctuation"
    if item.severity == "typo" or (
        item.severity == "major" and _looks_like_misspelling(approved, suspect)
    ):
        return "spelling"
    return "wording"


# Marks the row listing the repeats of a running header/footer
# (core/completeness.py `furniture_rows`).
FURNITURE_NOTE = "repeated header/footer"


def is_formatting_extra(item: DiffItem) -> bool:
    """Whether ``item`` changes only the layout, not a word: spacing
    ("100 mg" / "100mg"), a list mark drawn with another glyph ("ο" /
    "○"), a bullet present on one side only, or the repeats of a running
    header/footer (its first copy is compared with the text). Listed apart
    from the deviations, as formatting extras, and not counted with them
    -- never dropped."""
    if FURNITURE_NOTE in item.approved_text or FURNITURE_NOTE in item.suspect_text:
        return True
    if item.kind in ("added_in_suspect", "removed_in_suspect"):
        texts = [t for t in (item.approved_text, item.suspect_text) if t]
        return bool(texts) and all(
            all(word in _LIST_MARK_GLYPHS for word in text.split()) for text in texts
        )
    return _is_whitespace_only(item.approved_text, item.suspect_text) or _is_list_mark_only(
        item.approved_text, item.suspect_text
    )


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


def _letters(text: str) -> str:
    return "".join(ch for ch in text if ch.isalpha()).casefold()


# Glyph groups a PDF draws as ONE ligature and extraction can lose whole.
_LIGATURE_GROUPS = ("ffi", "ffl", "ff", "fi", "fl", "ft", "st", "ct")
_TYPO_MIN_LETTERS = 6
_TYPO_MAX_LOST_LETTERS = 3


def _looks_like_typo(approved_text: str, suspect_text: str) -> bool:
    """True if the difference has the shape of TEXT-LAYER NOISE rather
    than an edit: one side is the other with characters LOST, where every
    lost run is a doubled letter ("illness" -> "il ness",
    "hallucinations" -> "halucinations") or a ligature glyph
    ("specifically" -> "speci cally"), possibly with a stray space.

    Noise only ever loses characters. A letter that is ADDED or REPLACED
    ("contraindicated" -> "contrainsdicated", "LORATADIN" -> "LORATAZHIN")
    is a real misspelling and is reported at its own, higher tier (see
    `_looks_like_misspelling`). Never fires on text containing digits or a
    slash, or on words shorter than six letters, where one letter is
    routinely the whole meaning ("mg"/"mcg", "may"/"many").
    """
    if _has_digit_or_slash(approved_text) or _has_digit_or_slash(suspect_text):
        return False
    a, b = _letters(approved_text), _letters(suspect_text)
    if not a or not b or a == b:
        return False
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    if len(longer) - len(shorter) > _TYPO_MAX_LOST_LETTERS:
        return False
    if len(longer) < _TYPO_MIN_LETTERS:
        # A short word qualifies only for a whole lost ligature glyph
        # ("first" -> "rst"), which no real edit ever produces.
        return len(longer) >= 4 and any(
            longer.startswith(group, i) and longer[:i] + longer[i + len(group):] == shorter
            for group in _LIGATURE_GROUPS if len(group) >= 2
            for i in range(len(longer))
        )
    return _reducible_by_noise(longer, shorter)


@functools.lru_cache(maxsize=4096)
def _reducible_by_noise(longer: str, shorter: str) -> bool:
    """Whether ``shorter`` is ``longer`` with only doubled letters and
    ligature glyphs taken out (tried at every position, so an ambiguous
    alignment -- "speci|fi|cally" vs "spec|if|ically" -- cannot hide it)."""
    if longer == shorter:
        return True
    if len(longer) <= len(shorter):
        return False
    for i in range(len(longer)):
        if i + 1 < len(longer) and longer[i] == longer[i + 1]:
            if _reducible_by_noise(longer[:i] + longer[i + 1:], shorter):
                return True
        for group in _LIGATURE_GROUPS:
            if longer.startswith(group, i) and len(longer) - len(group) >= len(shorter):
                if _reducible_by_noise(longer[:i] + longer[i + len(group):], shorter):
                    return True
    return False


def _looks_like_misspelling(approved_text: str, suspect_text: str) -> bool:
    """True if the two sides are recognizably the SAME word(s) spelled
    differently -- close in length and highly similar letter for letter,
    with no digits involved and no change of meaning (see
    `_changes_meaning`). Reported as "major" in the Spelling category: a
    misspelling on a label is a real defect, just not a changed fact."""
    if _has_digit_or_slash(approved_text) or _has_digit_or_slash(suspect_text):
        return False
    if _changes_meaning(approved_text, suspect_text):
        return False
    # The word that differs, the words both sides share at either end left
    # out: "severe heart failure" / "ever had heart failure" read as one
    # misspelling once the shared words made the letters alike.
    words_a, words_b = approved_text.split(), suspect_text.split()
    while words_a and words_b and words_a[0] == words_b[0]:
        words_a, words_b = words_a[1:], words_b[1:]
    while words_a and words_b and words_a[-1] == words_b[-1]:
        words_a, words_b = words_a[:-1], words_b[:-1]
    if not words_a or not words_b or max(len(words_a), len(words_b)) > _MISSPELLING_MAX_WORDS:
        return False
    if len(words_a) == len(words_b):
        # Word for word ("Состдв: Кажда" / "Состав: Каждая").
        pairs = [(x, y) for x, y in zip(words_a, words_b) if _letters(x) != _letters(y)]
    else:
        # A word the text layer split or joined ("cipro oxacin" / "ciprooxacsin").
        pairs = [("".join(words_a), "".join(words_b))]
    return bool(pairs) and all(_is_slip(_letters(x), _letters(y)) for x, y in pairs)


def _is_slip(a: str, b: str) -> bool:
    """Whether ``b`` is ``a`` with a letter or two added, lost, changed or
    swapped -- not a different word: "for" / "of", "dose" / "dosage" are
    wording; "fom" / "from", "anhydrase" / "anhydrse" are slips."""
    if not a or not b or a == b or min(len(a), len(b)) < _MISSPELLING_MIN_LETTERS:
        return False
    allowed = 1 if min(len(a), len(b)) < _MISSPELLING_LONG_WORD else 2
    if _edit_distance(a, b) > allowed:
        return False
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= _TYPO_SIMILARITY_THRESHOLD


# A misspelling is a slip in a word of at least this many letters: one
# letter (two, when the shorter spelling has `_MISSPELLING_LONG_WORD`+
# letters) added, lost, changed or swapped with its neighbour.
_MISSPELLING_MIN_LETTERS = 3
_MISSPELLING_LONG_WORD = 5
# Differing words judged as a misspelling, at most, on each side.
_MISSPELLING_MAX_WORDS = 3


def _edit_distance(a: str, b: str) -> int:
    """Letters added, lost, changed, or swapped with a neighbour to turn
    ``a`` into ``b`` (optimal string alignment distance)."""
    rows = [list(range(len(b) + 1))]
    for i in range(1, len(a) + 1):
        row = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            row[j] = min(rows[-1][j] + 1, row[j - 1] + 1, rows[-1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                row[j] = min(row[j], rows[-2][j - 2] + 1)
        rows.append(row)
    return rows[-1][-1]


def _has_digit_or_slash(text: str) -> bool:
    return any(ch.isdigit() or ch == "/" for ch in text)


# Prefixes that turn a word into its opposite or into a different
# condition. A difference that is exactly one of these is never a
# spelling slip, however similar the letters look.
_OPPOSITE_PREFIXES = (
    ("hyper", "hypo"), ("inter", "intra"), ("ante", "anti"), ("pre", "post"),
    ("sub", "supra"), ("over", "under"), ("in", "ex"), ("endo", "exo"), ("hyper", "normo"),
)
_NEGATION_PREFIXES = ("non", "un", "in", "im", "ir", "il", "dis", "anti", "contra")
_CONTRACTION_RE = re.compile(r"n['\u2019]t\b|\bnot\b|\bno\b|\bnever\b", re.I)


def _changes_meaning(approved_text: str, suspect_text: str) -> bool:
    """Whether a one-word difference is a change of MEANING that merely
    looks like a spelling slip: a negation added or removed ("should" /
    "shouldn't", "compatible" / "incompatible"), or a prefix swapped for
    its opposite ("hypertension" / "hypotension")."""
    if bool(_CONTRACTION_RE.search(approved_text)) != bool(_CONTRACTION_RE.search(suspect_text)):
        return True
    a, b = _letters(approved_text), _letters(suspect_text)
    if not a or not b or a == b:
        return False
    for longer, shorter in ((a, b), (b, a)):
        for prefix in _NEGATION_PREFIXES:
            if longer.startswith(prefix) and longer[len(prefix):] == shorter:
                return True
    for first, second in _OPPOSITE_PREFIXES:
        for x, y in ((a, b), (b, a)):
            if x.startswith(first) and y.startswith(second) and len(x) > len(first) + 2:
                stem_x, stem_y = x[len(first):], y[len(second):]
                if stem_x == stem_y or difflib.SequenceMatcher(None, stem_x, stem_y).ratio() >= 0.85:
                    return True
    return False
