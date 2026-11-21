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

A difference of a word or two is rematched the same way: its own
coordinates say which words it is, and its PARAGRAPH is what is
matched, never the word on its own -- so a stray "4" elsewhere in the
other document can never stand in for it. Inside the partner paragraph
it is found by the words around it (`_counterpart_by_anchors`): the
words just before it and just after it, located in the partner, bracket
what stands there in its place.

`rematch_areas` is the manual version, for when the reviewer knows
better than any pairing: an area drawn on each document is compared
with the other, and nothing else. Every row touching either area is
replaced by that comparison; text that was paired with something
outside the areas is reported unpaired rather than silently dropped.

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
    _caret_location,
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

# A text SEARCH needs a passage, not a word: one or two words occur all
# over a document, and demoting a one-word dosage change because the
# other document also contains a "4" somewhere would hide exactly what
# this tool exists to catch. Only a row with no coordinates is found by
# searching for its text; one with coordinates is pinned to its own
# words whatever their number, and matched through its paragraph.
_MIN_QUERY_WORDS = 4

# Words taken from each side of the clicked text to find where it sits
# in the partner paragraph, and the fewest of them that must be found.
_ANCHOR_WORDS = 6
_MIN_ANCHOR_WORDS = 2
# How far (in words) from where the alignment of the two paragraphs
# puts the counterpart the anchors are looked for first.
_ANCHOR_REACH = 20

# A short difference is re-paired only with text clearly closer to it
# (character likeness, 0..1) than what it was paired with; a removed or
# added one only with text at least this alike.
_BETTER_BY = 0.15
_ONE_SIDED_LIKENESS = 0.5

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
        self._centres: dict[str, dict[int, list[tuple[int, float, float]]]] = {}
        chunks_a = _split_sections(approved_tokens) or []
        chunks_b = _split_sections(suspect_tokens) or []
        if approved_tokens and suspect_tokens and not (chunks_a and chunks_b):
            # Too little text to chunk (the comparison diffs it as one
            # stream): each document is then its own single paragraph.
            chunks_a = [_Section(tokens=list(approved_tokens), title="")]
            chunks_b = [_Section(tokens=list(suspect_tokens), title="")]
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

    def centres(self, side: str) -> dict[int, list[tuple[int, float, float]]]:
        """Where every word of that document sits (built on first use)."""
        if side not in self._centres:
            self._centres[side] = _word_centres(self.tokens[side])
        return self._centres[side]

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
    partners: Optional[dict[str, list[int]]] = None,
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
        partners: The comparison's word pairing (`TokenMap.partners`),
            to place text left unpaired at its insertion point in the
            other document.

    Returns a `Rematch` whose ``items`` replace this one row.
    """
    side = _usable_side(item, side)
    if side is None:
        return Rematch([item], False, 0.0, message="This row has no text to search for.")
    context = context or RematchContext(approved_tokens, suspect_tokens)
    own_tokens = context.tokens[side]
    other = other_side(side)
    other_name = "Suspect" if side == APPROVED else "Original"

    span, by_position = _span_tokens(own_tokens, _own_location(item, side), _own_text(item, side))
    if not span:
        return Rematch(
            [item], False, 0.0, message="Could not locate this text in its own document."
        )
    if not by_position and len(span) < _MIN_QUERY_WORDS:
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
    # The words around the clicked text pin down its place more surely
    # than the alignment of two whole paragraphs, which can pair a short
    # phrase with the same words further along.
    anchored = _counterpart_by_anchors(
        own_chunk.tokens, own_from, own_to, partner_chunk.tokens, (partner_from, partner_to)
    )
    if anchored is not None:
        partner_from, partner_to = anchored
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
    if len(rows) == 1 and _same_row(rows[0], item):
        # The same difference again: say so and leave the row (and any
        # decision already taken on it) as it is.
        return Rematch(
            [item],
            False,
            score,
            location,
            message=(
                f"Checked again against {which}{where} ({alike}): "
                "the difference stands as reported."
            ),
        )
    if rows and len(span) < _MIN_QUERY_WORDS and not _pairs_better(rows, item):
        # A word or two re-paired with something no closer to it than
        # what it was paired with is not a better answer, just another
        # one: the reported difference stands.
        return Rematch(
            [item],
            False,
            score,
            location,
            message=(
                f"Checked again by the words around it: nothing in the {other_name} reads "
                "closer to this text than what it is paired with -- the difference stands as reported."
            ),
        )

    # The row being replaced paired this text with something in the
    # other document. Whatever of that is not the counterpart now has
    # no partner: it is listed as removed/added, never silently dropped.
    leftover = _left_without_partner(context, item, other, counterpart)
    unpaired = sum(len(run) for run in leftover)
    note = (
        f" {unpaired} word{'s' if unpaired != 1 else ''} of the {other_name} it was paired "
        f"with {'is' if unpaired == 1 else 'are'} now listed as unpaired."
        if unpaired
        else ""
    )
    if leftover:
        extra: list[DiffItem] = []
        for run in leftover:
            extra += _unpaired_rows(context.tokens, other, run, partners)
        extra = _finalize_items(extra, approved_tokens, suspect_tokens)
    else:
        extra = []

    if not rows:
        return Rematch(
            extra,
            True,
            score,
            location,
            message=(
                f"No difference here after all: this text matches {which}{where} "
                f"({alike}). The row has been removed." + note
            ),
            repaired=repaired,
        )
    rows = rows + extra
    noun = "difference" if len(rows) == 1 else "differences"
    return Rematch(
        rows,
        True,
        score,
        location,
        message=f"Rematched against {which}{where} ({alike}): {len(rows)} {noun}." + note,
        repaired=repaired,
    )


def _left_without_partner(
    context: RematchContext, item: DiffItem, other: str, counterpart: list[ComparisonToken]
) -> list[list[int]]:
    """The words on the ``other`` side of the row being replaced that
    are not part of the text it is now paired with, as runs of
    consecutive token indices."""
    location = _location_of_side(item, other)
    if location is None:
        return []
    tokens = context.tokens[other]
    taken = {id(token) for token in counterpart}
    indices = _indices_in_boxes(context.centres(other), _location_boxes(location), pad=1.0)
    return _runs([i for i in indices if id(tokens[i]) not in taken])


# How far (in words) to look either side of an unpaired run for a word
# with a partner, to place the run's insertion point in the other document.
_CARET_SEARCH = 300


def _unpaired_rows(
    tokens: dict[str, list[ComparisonToken]],
    side: str,
    run: list[int],
    partners: Optional[dict[str, list[int]]],
) -> list[DiffItem]:
    """Rows for a run of words on ``side`` left with no partner: removed
    (Original) or added (Suspect) -- with the point in the OTHER
    document where they would belong, next to the partner of the
    nearest paired word around them.

    Without that point the viewer can only mirror the words' own boxes
    onto the other document, where the same coordinates land on
    unrelated text -- the dashed boxes over the Suspect's contents.
    """
    run_tokens = [tokens[side][i] for i in run]
    rows = _diff_runs(run_tokens, []) if side == APPROVED else _diff_runs([], run_tokens)
    caret = _caret_for_run(tokens, side, run, partners)
    for row in rows:
        if side == APPROVED:
            row.suspect_caret = row.suspect_caret or caret
        else:
            row.approved_caret = row.approved_caret or caret
    return rows


def _caret_for_run(
    tokens: dict[str, list[ComparisonToken]],
    side: str,
    run: list[int],
    partners: Optional[dict[str, list[int]]],
) -> Optional[Location]:
    if not partners or not run:
        return None
    mine = partners[side]
    other_tokens = tokens[other_side(side)]
    for index in range(run[0] - 1, max(-1, run[0] - 1 - _CARET_SEARCH), -1):
        if 0 <= index < len(mine) and mine[index] >= 0:
            after = mine[index] + 1  # right after the partner of the word before
            return _caret_location(other_tokens, after, after)
    for index in range(run[-1] + 1, min(len(mine), run[-1] + 1 + _CARET_SEARCH)):
        if mine[index] >= 0:
            return _caret_location(other_tokens, mine[index], mine[index])
    return None


def _pairing_after(
    partners: dict[str, list[int]],
    selected: dict[str, list[int]],
    pairs: list[tuple[int, int]],
) -> dict[str, list[int]]:
    """``partners`` as `TokenMap.rebind` will leave it: the selected
    words let go of their old partners and take the new ``pairs``."""
    after = {side: list(values) for side, values in partners.items()}
    for side, indices in selected.items():
        mine, theirs = after[side], after[other_side(side)]
        for index in indices:
            partner = mine[index] if index < len(mine) else -1
            if partner >= 0 and partner < len(theirs) and theirs[partner] == index:
                theirs[partner] = -1
            if index < len(mine):
                mine[index] = -1
    for i, j in pairs:
        after[APPROVED][i] = j
        after[SUSPECT][j] = i
    return after


# An area drawn on a page: (page, (x0, top, x1, bottom)) in PDF points,
# one entry per rectangle (a passage can take several -- two columns,
# or the foot of one page and the top of the next).
Area = list[tuple[int, tuple[float, float, float, float]]]


@dataclass
class AreaRematch:
    """The outcome of comparing two drawn areas with each other."""

    ok: bool  # the comparison ran (both areas held text)
    replaced: list[DiffItem]  # rows the comparison takes over
    items: list[DiffItem]  # the rows that replace them
    # Identical words paired by the comparison, (approved index,
    # suspect index): the new correspondence inside the areas.
    pairs: list[tuple[int, int]]
    selected: dict[str, list[int]]  # token indices inside each area
    message: str
    unpaired: int = 0  # words left with no partner by the new pairing


def rematch_areas(
    items: list[DiffItem],
    approved_tokens: list[ComparisonToken],
    suspect_tokens: list[ComparisonToken],
    approved_area: Area,
    suspect_area: Area,
    partners: Optional[dict[str, list[int]]] = None,
) -> AreaRematch:
    """Compare the text inside ``approved_area`` with the text inside
    ``suspect_area``, and nothing else: the reviewer saying "this is
    what this corresponds to".

    Every row with text inside either area is replaced by the rows this
    comparison produces. Nothing is silently lost on the way:

      * a replaced row's text that lies OUTSIDE the areas (a pairing
        that reached out of them) is reported again on its own, as
        removed or added;
      * text outside the areas that the comparison had paired, word for
        word, with text inside them (``partners``: per side, each
        token's partner index or -1) has lost its partner, and is
        reported as removed or added too.

    Args:
        items: Every row currently listed.
        approved_tokens / suspect_tokens: Both documents' token streams.
        approved_area / suspect_area: The areas drawn on each document.
        partners: The comparison's word pairing (`TokenMap.partners`).
    """
    tokens = {APPROVED: approved_tokens, SUSPECT: suspect_tokens}
    areas = {APPROVED: approved_area, SUSPECT: suspect_area}
    centres = {side: _word_centres(side_tokens) for side, side_tokens in tokens.items()}
    selected = {side: _indices_in_boxes(centres[side], _boxes_by_page(areas[side])) for side in tokens}
    empty = [name for side, name in ((APPROVED, "Original"), (SUSPECT, "Suspect")) if not selected[side]]
    if empty:
        return AreaRematch(
            False, [], [], [], selected,
            message=(
                f"No text inside the area drawn on the {' or the '.join(empty)}. "
                "Shift+drag around the words to compare."
            ),
        )

    inside = {side: set(indices) for side, indices in selected.items()}
    sel_a = [approved_tokens[i] for i in selected[APPROVED]]
    sel_b = [suspect_tokens[j] for j in selected[SUSPECT]]
    matched: list[tuple[ComparisonToken, ComparisonToken]] = []
    rows = _diff_runs(sel_a, sel_b, matched)
    position = {side: {id(token): i for i, token in enumerate(side_tokens)} for side, side_tokens in tokens.items()}
    pairs = [(position[APPROVED][id(a)], position[SUSPECT][id(b)]) for a, b in matched]

    # Rows the comparison takes over, and their text outside the areas.
    replaced: list[DiffItem] = []
    left_out: dict[str, set[int]] = {APPROVED: set(), SUSPECT: set()}
    pages = {side: {page for page, _box in areas[side]} for side in tokens}
    for item in items:
        locations = {side: _location_of_side(item, side) for side in tokens}
        # Cheap test first: only a row on a page of an area can touch it.
        if not any(
            location is not None and set(_location_boxes(location)) & pages[side]
            for side, location in locations.items()
        ):
            continue
        own = {
            side: (
                _indices_in_boxes(centres[side], _location_boxes(location), pad=1.0, as_set=True)
                if location is not None
                else set()
            )
            for side, location in locations.items()
        }
        if not (own[APPROVED] & inside[APPROVED] or own[SUSPECT] & inside[SUSPECT]):
            continue
        replaced.append(item)
        for side in tokens:
            left_out[side] |= own[side] - inside[side]

    # Words outside the areas paired with words inside them.
    if partners:
        for side in tokens:
            other = other_side(side)
            for index in selected[side]:
                partner = partners[side][index] if index < len(partners[side]) else -1
                if partner >= 0 and partner not in inside[other]:
                    left_out[other].add(partner)

    # The pairing as it will be once this rematch is applied: where an
    # unpaired word's insertion point in the other document is.
    after = _pairing_after(partners, selected, pairs) if partners else None
    unpaired = 0
    for side in (APPROVED, SUSPECT):
        for run in _runs(sorted(left_out[side])):
            unpaired += len(run)
            rows += _unpaired_rows(tokens, side, run, after)

    rows = _finalize_items(rows, approved_tokens, suspect_tokens)
    words = f"{len(sel_a)} word{'s' if len(sel_a) != 1 else ''} ↔ {len(sel_b)}"
    if rows:
        noun = "difference" if len(rows) == 1 else "differences"
        message = f"Compared the two areas ({words}): {len(rows)} {noun}"
    else:
        message = f"Compared the two areas ({words}): they match word for word"
    if replaced:
        message += f", replacing {len(replaced)} row{'s' if len(replaced) != 1 else ''}"
    message += "."
    if unpaired:
        message += (
            f" {unpaired} word{'s' if unpaired != 1 else ''} outside the areas lost "
            "{} partner and {} listed as unpaired.".format(
                "its" if unpaired == 1 else "their", "is" if unpaired == 1 else "are"
            )
        )
    return AreaRematch(True, replaced, rows, pairs, selected, message, unpaired)


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _location_of_side(item: DiffItem, side: str) -> Optional[Location]:
    return item.approved_location if side == APPROVED else item.suspect_location


def _word_centres(tokens: list[ComparisonToken]) -> dict[int, list[tuple[int, float, float]]]:
    """page -> [(token index, x, y)] for the centre of every source word."""
    pages: dict[int, list[tuple[int, float, float]]] = {}
    for index, token in enumerate(tokens):
        for word in token.source_words:
            x0, top, x1, bottom = word.bbox
            pages.setdefault(word.page, []).append((index, (x0 + x1) / 2.0, (top + bottom) / 2.0))
    return pages


def _boxes_by_page(area: Area) -> dict[int, list[tuple[float, float, float, float]]]:
    pages: dict[int, list[tuple[float, float, float, float]]] = {}
    for page, box in area:
        pages.setdefault(page, []).append(box)
    return pages


def _location_boxes(location: Location) -> dict[int, list[tuple[float, float, float, float]]]:
    if location.boxes_by_page:
        return location.boxes_by_page
    return {location.page: location.boxes or [location.bbox]}


def _indices_in_boxes(
    centres: dict[int, list[tuple[int, float, float]]],
    boxes_by_page: dict[int, list[tuple[float, float, float, float]]],
    pad: float = 0.0,
    as_set: bool = False,
):
    """Indices of the tokens with a word centred inside the boxes, in
    reading order (or as a set)."""
    found: set[int] = set()
    for page, boxes in boxes_by_page.items():
        for index, x, y in centres.get(page, ()):
            for x0, top, x1, bottom in boxes:
                if x0 - pad <= x <= x1 + pad and top - pad <= y <= bottom + pad:
                    found.add(index)
                    break
    return found if as_set else sorted(found)


def _runs(indices: list[int]) -> list[list[int]]:
    """Consecutive stretches of sorted token indices."""
    runs: list[list[int]] = []
    for index in indices:
        if runs and index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    return runs


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
        if tag == "equal" or (tag == "replace" and own_end - own_start == other_end - other_start):
            # One for one -- identical words, or as many words replaced
            # by as many ("Olmesartan Medoxomil, Amlodipine" against
            # "olmesartan medoxomil, amlodipine"): map the overlap exactly.
            start = other_start + max(own_from - own_start, 0)
            end = other_start + min(own_to - own_start, own_end - own_start)
        else:
            start, end = other_start, other_end
        low = start if low is None else min(low, start)
        high = end if high is None else max(high, end)
    if low is None or high is None or high < low:
        return 0, 0
    return low, high


def _counterpart_by_anchors(
    own: list[ComparisonToken],
    own_from: int,
    own_to: int,
    partner: list[ComparisonToken],
    guess: tuple[int, int],
) -> Optional[tuple[int, int]]:
    """Where the clicked text sits in the partner paragraph, found by
    the words around it: the words just before it and just after it,
    located in the partner nearest to where the alignment put them.
    What stands between the two is the counterpart (nothing, when the
    text was simply added or removed there).

    None when the surroundings are not found on both sides, are too
    far apart to bracket text of this length, or two equally near
    placements disagree -- the alignment's answer then stands.
    """
    # Words only: a stray "(" or ")" split off differently in the two
    # documents must not break the surroundings apart.
    own_keys = [_anchor_key(token.text) for token in own]
    left = [key for key in own_keys[:own_from] if key][-_ANCHOR_WORDS:]
    right = [key for key in own_keys[own_to:] if key][:_ANCHOR_WORDS]
    if len(left) < _MIN_ANCHOR_WORDS or len(right) < _MIN_ANCHOR_WORDS:
        return None
    words = [(index, key) for index, key in enumerate(_anchor_key(token.text) for token in partner) if key]
    keys = [key for _index, key in words]
    # Positions among the words -> positions in the partner paragraph.
    ends = [(words[end - 1][0] + 1, size) for end, size in _anchor_positions(keys, left, before=True)]
    starts = [
        (words[start][0] if start < len(words) else len(partner), size)
        for start, size in _anchor_positions(keys, right, before=False)
    ]
    if not ends or not starts:
        return None
    longest = 2 * (own_to - own_from) + 8
    guess_from, guess_to = guess
    low, high = guess_from - _ANCHOR_REACH, guess_to + _ANCHOR_REACH
    texts = [token.text for token in partner]

    near: list[tuple[int, int, int, int]] = []  # (-strength, cost, end, start)
    far: list[tuple[int, int]] = []
    for end, left_size in ends:
        for start, right_size in starts:
            if start < end or start - end > longest:
                continue
            if low <= end <= high and low <= start <= high:
                cost = abs(end - guess_from) + abs(start - guess_to)
                near.append((-(left_size + right_size), cost, end, start))
            elif left_size == right_size == _ANCHOR_WORDS:
                far.append((end, start))

    if near:
        near.sort()
        best = near[0]
        rivals = [c for c in near[1:] if c[:2] == best[:2] and texts[c[2] : c[3]] != texts[best[2] : best[3]]]
        return None if rivals else (best[2], best[3])
    # Nothing around the place the alignment pointed at. Somewhere else
    # in the paragraph, only the full surroundings on BOTH sides -- and
    # only one such place -- are trusted: a sentence reworded before it
    # reached this spot, not a look-alike sharing a couple of words.
    if len({tuple(texts[end:start]) for end, start in far}) == 1:
        return far[0]
    return None


def _anchor_key(text: str) -> str:
    """A word as the anchors compare it: case and punctuation do not
    move a sentence, so they do not stop its words locating it. (The
    comparison itself stays exact.)"""
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _anchor_positions(keys: list[str], context: list[str], before: bool) -> list[tuple[int, int]]:
    """Where ``context`` -- the words just before the clicked text when
    ``before``, else just after it -- occurs in ``keys``, as (the
    position the clicked text would start (before) or end (after) at,
    how many of its words were found there). Every length from the
    whole context down to `_MIN_ANCHOR_WORDS` is tried, each place
    counted once with its longest match."""
    found: dict[int, int] = {}
    for size in range(len(context), _MIN_ANCHOR_WORDS - 1, -1):
        gram = context[-size:] if before else context[:size]
        for index in range(len(keys) - size + 1):
            if keys[index : index + size] == gram:
                position = index + size if before else index
                found.setdefault(position, size)
    return list(found.items())


def _likeness(approved: str, suspect: str) -> float:
    if not approved or not suspect:
        return 0.0
    return difflib.SequenceMatcher(None, approved.lower(), suspect.lower(), autojunk=False).ratio()


def _pairs_better(rows: list[DiffItem], old: DiffItem) -> bool:
    """Whether re-paired rows put a short text against something
    clearly closer to it than the row it replaces did -- "7." against
    "7:" instead of "8:". A one-sided row (text removed or added) is
    only turned into a change when the two sides read alike."""
    new = _likeness(
        " ".join(row.approved_text for row in rows if row.approved_text),
        " ".join(row.suspect_text for row in rows if row.suspect_text),
    )
    before = _likeness(old.approved_text, old.suspect_text)
    if not (old.approved_text and old.suspect_text):
        return new >= _ONE_SIDED_LIKENESS
    return new >= before + _BETTER_BY


def _same_row(new: DiffItem, old: DiffItem) -> bool:
    return (new.approved_text, new.suspect_text) == (old.approved_text, old.suspect_text)


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
) -> tuple[list[ComparisonToken], bool]:
    """The tokens this discrepancy's text is made of, found by its own
    coordinates; falls back to the one place the text occurs when there
    are no usable coordinates. The flag says whether the coordinates
    found them (True) or a text search did (False)."""
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
            return tokens[min(indices) : max(indices) + 1], True
    words = text.split()
    if not words:
        return [], False
    texts = [t.text for t in tokens]
    starts = [
        i for i in range(len(texts) - len(words) + 1) if texts[i : i + len(words)] == words
    ]
    return (tokens[starts[0] : starts[0] + len(words)] if len(starts) == 1 else []), False
