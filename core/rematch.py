"""Re-compare ONE discrepancy, on the structure (core/rematch.py).

`core.diff_engine` matches the two documents as wholes. Every chunk --
section or paragraph -- competes with every other for a partner, and
one chunk that wins a partner it should not have leaves the rightful
owner to pair with whatever is left. The result is a difference that is
not one: two paragraphs reported as wholly rewritten when the real
story is that their partners got swapped.

Rematch is the reviewer's answer, for the one difference under the
cursor. It works on exactly the structure the comparison works on --
the same sections and paragraphs, the same pairing, the same reading
order -- and changes only one thing: this chunk is no longer competing
with the rest of the document for its partner.

    1. Find the SECTION or PARAGRAPH the clicked text belongs to
       (`core.diff_engine._split_sections` -- the same chunking the
       comparison used).
    2. Work out WHERE its partner has to be: between the partners of
       the nearest paired chunks before and after it. Reading order is
       the constraint, so a paragraph cannot be rematched to one 40
       pages away.
    3. Pick the best partner in that range on its own merits, with no
       other chunk competing for it -- which is how a partner that was
       taken by a look-alike comes back.
    4. Diff that one pair of chunks in isolation, and report what
       really differs where the reviewer clicked.

Which side was double-clicked decides which chunk is rematched, so
"where does my Original paragraph really belong?" and "where does this
Suspect paragraph come from?" are separate questions.

Outcomes, in the reviewer's terms:

  * the paragraph matches its counterpart after all -> the row goes
    away, with an explanation;
  * it matches a DIFFERENT counterpart in the same part of the
    document -> the real differences against that one;
  * nothing in that part of the document is a plausible counterpart
    -> the row stands, now confirmed.

`RematchContext` holds the chunking and pairing so that repeated
rematches on one comparison cost nothing after the first.

No Qt: standalone-testable.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Optional

from core.diff_engine import (
    DiffItem,
    Location,
    _diff_runs,
    _finalize_items,
    _location_of,
    _match_sections,
    _opcodes,
    _Section,
    _split_sections,
)
from core.pdf_extractor import ComparisonToken

APPROVED = "approved"
SUSPECT = "suspect"

# A chunk must be at least this alike to be accepted as the partner of
# the one being rematched. Below it, nothing in that part of the
# document is a plausible counterpart and the difference stands.
_MIN_CHUNK_RATIO = 0.3

# A search needs a passage, not a word: one or two words occur all over
# a document, and demoting a one-word dosage change because the other
# document also contains a "4" somewhere would hide exactly what this
# tool exists to catch.
_MIN_QUERY_WORDS = 4

# With no paired chunk before or after to bound the search (two
# documents with nothing in common yet), the partner is looked for
# within this many chunks of the expected position rather than
# anywhere.
_POSITION_SLACK = 3

# How much better a chunk OUTSIDE the position the structure allows has
# to be before it beats one in the right place. A paragraph repeated
# verbatim elsewhere in the document is a perfect match on text and the
# wrong answer on position; only a far better match wins the argument.
_POSITION_MARGIN = 0.25


@dataclass
class Rematch:
    """The outcome of re-comparing one discrepancy on its own."""

    items: list[DiffItem]  # the rows that replace the one rematched
    found: bool  # a counterpart chunk was identified
    score: float  # how alike the two chunks are (0..1)
    location: Optional[Location] = None  # where the counterpart sits
    message: str = ""  # what to tell the reviewer
    repaired: bool = False  # the pairing changed, not just the rows


class RematchContext:
    """The document structure a rematch works on: the same chunks and
    the same pairing the comparison itself used.

    Built once per comparison -- `_match_sections` is the expensive
    part -- and reused for every rematch on it.
    """

    def __init__(
        self,
        approved_tokens: list[ComparisonToken],
        suspect_tokens: list[ComparisonToken],
    ) -> None:
        self.tokens = {APPROVED: approved_tokens, SUSPECT: suspect_tokens}
        chunks_a = _split_sections(approved_tokens) or []
        chunks_b = _split_sections(suspect_tokens) or []
        self.chunks = {APPROVED: chunks_a, SUSPECT: chunks_b}
        # Which chunk each token belongs to, for finding the clicked
        # text's own paragraph.
        self.chunk_of_token = {
            side: {
                id(token): index
                for index, chunk in enumerate(chunks)
                for token in chunk.tokens
            }
            for side, chunks in self.chunks.items()
        }
        if chunks_a and chunks_b:
            b_to_a, _unmatched_a, _unmatched_b = _match_sections(chunks_a, chunks_b)
        else:
            b_to_a = {}
        # The comparison's own pairing, both ways round.
        self.partner = {
            APPROVED: {a_idx: b_idx for b_idx, a_idx in b_to_a.items()},
            SUSPECT: dict(b_to_a),
        }

    def chunk_index(self, side: str, token: ComparisonToken) -> Optional[int]:
        return self.chunk_of_token[side].get(id(token))

    def chunk(self, side: str, index: int) -> _Section:
        return self.chunks[side][index]

    def candidates(self, side: str, index: int) -> list[int]:
        """Chunks on the other side that sit where this one's partner
        has to sit: between the partners of the nearest paired chunks
        before and after it.

        This is the position rule. A paragraph sits where it sits; its
        counterpart is between its neighbours' counterparts, not
        anywhere in the document that happens to read the same.
        """
        other = other_side(side)
        partners = self.partner[side]
        count = len(self.chunks[other])
        if not count:
            return []

        low = None
        for before in range(index - 1, -1, -1):
            if before in partners:
                low = partners[before]
                break
        high = None
        for after in range(index + 1, len(self.chunks[side])):
            if after in partners:
                high = partners[after]
                break

        if low is None and high is None:
            # Nothing paired anywhere: stay near the same relative
            # position rather than searching the whole document.
            expected = round(index * count / max(len(self.chunks[side]), 1))
            start, end = expected - _POSITION_SLACK, expected + _POSITION_SLACK + 1
        else:
            start = 0 if low is None else low + 1
            end = count if high is None else high
        return list(range(max(0, start), min(count, max(end, 0))))


def other_side(side: str) -> str:
    return SUSPECT if side == APPROVED else APPROVED


def rematch_item(
    item: DiffItem,
    approved_tokens: list[ComparisonToken],
    suspect_tokens: list[ComparisonToken],
    side: str = APPROVED,
    context: Optional[RematchContext] = None,
) -> Rematch:
    """Re-compare ``item`` alone, on the document structure.

    Args:
        item: The flagged discrepancy to re-check.
        approved_tokens / suspect_tokens: Both documents' full token
            streams (the ones the comparison ran on).
        side: Which side of the row was double-clicked -- that text's
            own paragraph is the one rematched.
        context: A `RematchContext` for these two documents; built here
            when not supplied (and worth keeping between rematches).

    Returns a `Rematch` whose ``items`` replace this one row.
    """
    side = _usable_side(item, side)
    if side is None:
        return Rematch([item], False, 0.0, message="This row has no text to search for.")

    context = context or RematchContext(approved_tokens, suspect_tokens)
    own_tokens = context.tokens[side]
    other = other_side(side)
    other_name = "Suspect" if side == APPROVED else "Original"

    span = _span_tokens(own_tokens, _own_location(item, side), _own_text(item, side))
    if not span:
        return Rematch(
            [item], False, 0.0, message="Could not locate this text in its own document."
        )
    if len(span) < _MIN_QUERY_WORDS:
        return Rematch(
            [item],
            False,
            0.0,
            message=(
                f"Too short to rematch on its own ({len(span)} "
                f"word{'' if len(span) == 1 else 's'}): a word or two occurs all over a "
                "document. Rematch a whole sentence or paragraph instead."
            ),
        )

    index = context.chunk_index(side, span[0])
    if index is None:
        return Rematch(
            [item], False, 0.0,
            message="This text is not inside a paragraph the comparison chunked.",
        )
    own_chunk = context.chunk(side, index)

    best_index, score = _best_partner(context, side, index)
    if best_index is None or score < _MIN_CHUNK_RATIO:
        return Rematch(
            [item],
            False,
            score,
            message=(
                f"Nothing in this part of the {other_name} is a plausible counterpart "
                "for this paragraph -- the difference is genuine."
            ),
        )

    partner_chunk = context.chunk(other, best_index)
    was = context.partner[side].get(index)
    repaired = was != best_index
    location = _location_of(partner_chunk.tokens)
    page = location.page if location is not None else None

    # Align the two paragraphs, then compare ONLY the clicked text
    # against the text that sits at the same place in the partner. The
    # rest of the paragraph already has rows of its own.
    if side == APPROVED:
        chunk_a, chunk_b = own_chunk.tokens, partner_chunk.tokens
    else:
        chunk_a, chunk_b = partner_chunk.tokens, own_chunk.tokens
    own_from, own_to = _span_range(own_chunk.tokens, span)
    partner_from, partner_to = _corresponding_range(
        _opcodes(chunk_a, chunk_b), own_from, own_to, side == APPROVED
    )
    counterpart = partner_chunk.tokens[partner_from:partner_to]
    if side == APPROVED:
        rows = _diff_runs(span, counterpart)
    else:
        rows = _diff_runs(counterpart, span)
    rows = _finalize_items(rows, approved_tokens, suspect_tokens)
    if counterpart:
        location = _location_of(counterpart) or location
        page = location.page if location is not None else page

    which = (
        f"a different paragraph of the {other_name}"
        if repaired
        else "the paragraph it was already paired with"
    )
    where = f" on page {page}" if page is not None else ""
    alike = f"{round(score * 100)}% alike"
    if not rows:
        return Rematch(
            [],
            True,
            score,
            location,
            message=(
                f"No difference here after all: this text matches {which}{where} "
                f"({alike}). The row has been removed."
            ),
            repaired=repaired,
        )
    noun = "difference" if len(rows) == 1 else "differences"
    return Rematch(
        rows,
        True,
        score,
        location,
        message=f"Rematched against {which}{where} ({alike}): {len(rows)} {noun}.",
        repaired=repaired,
    )


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _best_partner(
    context: RematchContext, side: str, index: int
) -> tuple[Optional[int], float]:
    """The chunk this one really belongs with: the best of those its
    POSITION allows, judged on its own with no other chunk competing
    for it.

    The partner it already has is considered too, but only wins if it
    is far better (`_POSITION_MARGIN`) than anything in the right
    place -- otherwise a paragraph repeated verbatim later in the
    document would keep claiming paragraphs from the top of it, which
    is the kind of pairing a reviewer rematches to get rid of.
    """
    own = [token.text for token in context.chunk(side, index).tokens]
    other = other_side(side)
    matcher = difflib.SequenceMatcher(None, autojunk=False)
    matcher.set_seq1(own)

    def ratio(candidate: int, floor: float) -> float:
        tokens = [token.text for token in context.chunk(other, candidate).tokens]
        if not tokens:
            return 0.0
        matcher.set_seq2(tokens)
        # The cheap bounds first: most candidates cannot come close.
        if matcher.real_quick_ratio() <= floor or matcher.quick_ratio() <= floor:
            return 0.0
        return matcher.ratio()

    in_place = context.candidates(side, index)
    best_index, best_score = None, 0.0
    for candidate in in_place:
        score = ratio(candidate, best_score)
        if score > best_score:
            best_index, best_score = candidate, score

    current = context.partner[side].get(index)
    if current is not None and current not in in_place:
        current_score = ratio(current, 0.0)
        if current_score >= best_score + _POSITION_MARGIN:
            return current, current_score
    return best_index, best_score


def _span_range(
    chunk_tokens: list[ComparisonToken], span: list[ComparisonToken]
) -> tuple[int, int]:
    """Where the clicked text sits inside its own paragraph."""
    positions = {id(token): index for index, token in enumerate(chunk_tokens)}
    start = positions.get(id(span[0]))
    end = positions.get(id(span[-1]))
    if start is None or end is None:
        return 0, len(chunk_tokens)
    return start, end + 1


def _corresponding_range(
    opcodes: list[tuple[str, int, int, int, int]],
    own_from: int,
    own_to: int,
    own_is_approved: bool,
) -> tuple[int, int]:
    """Where the clicked text's counterpart sits in the paired
    paragraph, read off the alignment of the two paragraphs.

    This is what makes the answer positional rather than a text hunt:
    the same words elsewhere in the paragraph are NOT the counterpart;
    whatever stands at this place in the partner is. An empty range
    means the partner has nothing there -- the text really was added.
    """
    low = high = None
    for tag, i1, i2, j1, j2 in opcodes:
        own_start, own_end = (i1, i2) if own_is_approved else (j1, j2)
        other_start, other_end = (j1, j2) if own_is_approved else (i1, i2)
        if own_end < own_from or own_start > own_to:
            continue
        if tag == "equal":
            # One for one: map the overlapping part exactly.
            start = other_start + max(own_from - own_start, 0)
            end = other_start + min(own_to - own_start, own_end - own_start)
        else:
            start, end = other_start, other_end
        low = start if low is None else min(low, start)
        high = end if high is None else max(high, end)
    if low is None or high is None or high < low:
        return 0, 0
    return low, high


def _usable_side(item: DiffItem, side: str) -> Optional[str]:
    """The side whose paragraph is rematched: the clicked one when it
    has text, else the one that does."""
    if side == APPROVED and item.approved_text:
        return APPROVED
    if side == SUSPECT and item.suspect_text:
        return SUSPECT
    if item.approved_text:
        return APPROVED
    if item.suspect_text:
        return SUSPECT
    return None


def _own_text(item: DiffItem, side: str) -> str:
    return item.approved_text if side == APPROVED else item.suspect_text


def _own_location(item: DiffItem, side: str) -> Optional[Location]:
    return item.approved_location if side == APPROVED else item.suspect_location


def _span_tokens(
    tokens: list[ComparisonToken], location: Optional[Location], text: str
) -> list[ComparisonToken]:
    """The tokens this discrepancy's text is made of, found by its own
    coordinates; falls back to the one place the text occurs when there
    are no usable coordinates."""
    if location is not None:
        pages = location.boxes_by_page or {location.page: location.boxes}
        indices: list[int] = []
        for index, token in enumerate(tokens):
            for word in token.source_words:
                boxes = pages.get(word.page)
                if not boxes:
                    continue
                x0, top, x1, bottom = word.bbox
                cx, cy = (x0 + x1) / 2.0, (top + bottom) / 2.0
                if any(
                    bx0 - 1.0 <= cx <= bx1 + 1.0 and btop - 1.0 <= cy <= bbottom + 1.0
                    for bx0, btop, bx1, bbottom in (boxes or [location.bbox])
                ):
                    indices.append(index)
                    break
        if indices:
            return tokens[min(indices) : max(indices) + 1]
    words = text.split()
    if not words:
        return []
    texts = [t.text for t in tokens]
    starts = [
        i for i in range(len(texts) - len(words) + 1) if texts[i : i + len(words)] == words
    ]
    return tokens[starts[0] : starts[0] + len(words)] if len(starts) == 1 else []
