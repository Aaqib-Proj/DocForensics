"""Word-level correspondence between the two compared documents.

Built once per comparison. Answers "the user clicked this word in one
document -- where is the same text in the other?", and never guesses:

  1. A word inside a run of at least ``_MIN_RELIABLE_RUN`` words the diff
     engine matched in sequence (`diff_documents(..., matches=...)`) maps
     to its matched partner.
  2. Otherwise the word's surrounding phrase (``_CONTEXT_WORDS`` words
     including it) is looked up in the other document; if that exact
     phrase occurs there exactly once -- or exactly once between the
     matched words bracketing the click -- the word maps into it. This
     finds text the diff alignment itself missed -- e.g. a paragraph a
     print layout moved into a different section chunk.
  3. Otherwise there is no counterpart. The UI then says so instead of
     jumping somewhere unrelated.

Positions come from the comparison, not page coordinates, so this stays
exact when the documents are laid out differently (different page
sizes, columns, a section moved to another page).

No Qt imports -- standalone-testable.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from core.diff_engine import Location, TokenMatches, _location_of
from core.pdf_extractor import ComparisonToken

APPROVED = "approved"
SUSPECT = "suspect"

EXACT = "exact"  # the diff engine matched this word
CONTEXT = "context"  # found by its surrounding phrase

# A click farther than this (PDF points) from every word hits nothing.
_MAX_HIT_DISTANCE = 40.0
# Matched runs shorter than this can be coincidences (a lone "and"
# paired across unrelated text), so they need the phrase check too.
_MIN_RELIABLE_RUN = 3
# Phrase length used to locate a word the alignment didn't match.
_CONTEXT_WORDS = 5
# How far (in words) to look for matched words bracketing a repeated phrase.
_MAX_ANCHOR_DISTANCE = 300
# Words of surrounding text shown around a discrepancy in the comparator.
_CONTEXT_SHOWN_WORDS = 4


def other_side(side: str) -> str:
    return SUSPECT if side == APPROVED else APPROVED


class TokenMap:
    def __init__(
        self,
        approved_tokens: list[ComparisonToken],
        suspect_tokens: list[ComparisonToken],
        matches: TokenMatches,
    ) -> None:
        self._tokens = {APPROVED: approved_tokens, SUSPECT: suspect_tokens}
        self._texts = {side: [t.text for t in tokens] for side, tokens in self._tokens.items()}
        index = {
            side: {id(token): i for i, token in enumerate(tokens)}
            for side, tokens in self._tokens.items()
        }
        self._partner = {side: [-1] * len(tokens) for side, tokens in self._tokens.items()}
        for token_a, token_b in matches:
            i = index[APPROVED].get(id(token_a))
            j = index[SUSPECT].get(id(token_b))
            if i is not None and j is not None:
                self._partner[APPROVED][i] = j
                self._partner[SUSPECT][j] = i
        self._run_length = {side: self._run_lengths(side) for side in self._tokens}

        # side -> phrase -> start positions; built on first use (clicks
        # on unmatched words), not with every comparison.
        self._phrases: dict[str, dict[tuple[str, ...], list[int]]] = {}

        # side -> page -> [(token index, (x0, top, x1, bottom)) per source word]
        self._by_page: dict[str, dict[int, list[tuple[int, tuple]]]] = {}
        for side, tokens in self._tokens.items():
            pages: dict[int, list[tuple[int, tuple]]] = {}
            for i, token in enumerate(tokens):
                for word in token.source_words:
                    pages.setdefault(word.page, []).append((i, word.bbox))
            self._by_page[side] = pages

    def _run_lengths(self, side: str) -> list[int]:
        """For each word, the length of the in-sequence matched run it
        belongs to (0 if unmatched)."""
        partners = self._partner[side]
        lengths = [0] * len(partners)
        i = 0
        while i < len(partners):
            if partners[i] < 0:
                i += 1
                continue
            end = i + 1
            while end < len(partners) and partners[end] == partners[end - 1] + 1:
                end += 1
            for k in range(i, end):
                lengths[k] = end - i
            i = end
        return lengths

    def token_at(
        self,
        side: str,
        page: int,
        x: float,
        y: float,
        max_distance: Optional[float] = _MAX_HIT_DISTANCE,
    ) -> Optional[int]:
        """Index of the token nearest to (x, y) in PDF points on ``page``,
        or None if none is within ``max_distance`` (None: no limit)."""
        best = None
        best_dist = float("inf") if max_distance is None else max_distance
        for i, (x0, top, x1, bottom) in self._by_page[side].get(page, ()):
            dx = max(x0 - x, 0.0, x - x1)
            # Vertical distance counts more: a click belongs to its line.
            dy = max(top - y, 0.0, y - bottom) * 2.0
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < best_dist:
                best, best_dist = i, dist
        return best

    def counterpart(self, side: str, index: int) -> tuple[Optional[int], Optional[str]]:
        """The same word in the other document: (index, EXACT or
        CONTEXT), or (None, None) when it can't be located reliably."""
        partner = self._partner[side][index]
        if partner >= 0 and self._run_length[side][index] >= _MIN_RELIABLE_RUN:
            return partner, EXACT
        by_context = self._by_context(side, index)
        if by_context is not None:
            return by_context, (EXACT if by_context == partner else CONTEXT)
        return None, None

    def _by_context(self, side: str, index: int) -> Optional[int]:
        """Locate ``index`` through the phrases around it in the other
        document. A phrase occurring there more than once (repeated
        wording) is narrowed to the occurrences lying between the
        partners of the nearest reliably matched words before and after
        ``index``. The result must be a single position."""
        texts = self._texts[side]
        other = self._phrase_index(other_side(side))
        unique: set[int] = set()
        repeated: set[int] = set()
        for start in range(index - _CONTEXT_WORDS + 1, index + 1):
            if start < 0 or start + _CONTEXT_WORDS > len(texts):
                continue
            positions = other.get(tuple(texts[start : start + _CONTEXT_WORDS]), ())
            candidates = {p + index - start for p in positions}
            (unique if len(positions) == 1 else repeated).update(candidates)
        if len(unique) == 1:
            return unique.pop()
        if unique or not repeated:
            return None  # phrases disagree, or none found
        low, high = self._anchor_bounds(side, index)
        if low is None or high is None:
            return None
        between = {c for c in repeated if low < c < high}
        return between.pop() if len(between) == 1 else None

    def _phrase_index(self, side: str) -> dict[tuple[str, ...], list[int]]:
        phrases = self._phrases.get(side)
        if phrases is None:
            texts = self._texts[side]
            phrases = defaultdict(list)
            for start in range(len(texts) - _CONTEXT_WORDS + 1):
                phrases[tuple(texts[start : start + _CONTEXT_WORDS])].append(start)
            self._phrases[side] = phrases
        return phrases

    def _anchor_bounds(
        self, side: str, index: int, max_distance: int = _MAX_ANCHOR_DISTANCE
    ) -> tuple[Optional[int], Optional[int]]:
        """Partners of the nearest reliably matched words before and after
        ``index`` (None if there is none within ``max_distance``, or they
        are out of order)."""
        partners = self._partner[side]
        runs = self._run_length[side]

        def nearest(step: int) -> Optional[int]:
            i = index + step
            while 0 <= i < len(partners) and abs(i - index) <= max_distance:
                if partners[i] >= 0 and runs[i] >= _MIN_RELIABLE_RUN:
                    return partners[i]
                i += step
            return None

        low, high = nearest(-1), nearest(+1)
        if low is not None and high is not None and low >= high:
            return None, None
        return low, high

    def nearby_counterpart(
        self, side: str, index: int, max_offset: int = 60
    ) -> tuple[Optional[int], Optional[int]]:
        """(this-side index, other-side index) of the closest word to
        ``index`` that has a reliable counterpart, for keeping locked
        views roughly in step over text that has none."""
        for offset in range(max_offset + 1):
            for i in {index - offset, index + offset}:
                if 0 <= i < len(self._partner[side]):
                    other, _kind = self.counterpart(side, i)
                    if other is not None:
                        return i, other
        return None, None

    def location(self, side: str, index: int) -> Optional[Location]:
        return _location_of([self._tokens[side][index]])

    def has_tokens(self, side: str) -> bool:
        return bool(self._tokens[side])

    # ------------------------------------------------------------------
    # Context around a discrepancy (for the comparator pane)
    # ------------------------------------------------------------------

    def span_at(self, side: str, location: Optional[Location]) -> Optional[tuple[int, int]]:
        """(first, last) token index of the text ``location`` covers, by
        matching each word's center against the location's own per-line
        boxes. None when the location covers no word (e.g. a caret at an
        insertion point, which sits BETWEEN words)."""
        if location is None:
            return None
        pages = location.boxes_by_page or {location.page: location.boxes}
        indices: list[int] = []
        for page, boxes in pages.items():
            page_boxes = boxes or [location.bbox]
            for i, (x0, top, x1, bottom) in self._by_page[side].get(page, ()):
                cx, cy = (x0 + x1) / 2.0, (top + bottom) / 2.0
                for bx0, btop, bx1, bbottom in page_boxes:
                    if bx0 - 1.0 <= cx <= bx1 + 1.0 and btop - 1.0 <= cy <= bbottom + 1.0:
                        indices.append(i)
                        break
        if not indices:
            return None
        return min(indices), max(indices)

    def context_words(
        self, side: str, location: Optional[Location], words: int = _CONTEXT_SHOWN_WORDS
    ) -> tuple[str, str]:
        """The text immediately before and after ``location``'s own text,
        so a discrepancy can be read in its sentence instead of on its
        own. ("", "") when the location can't be tied to any word."""
        span = self.span_at(side, location)
        if span is None:
            return "", ""
        texts = self._texts[side]
        first, last = span
        return (
            " ".join(texts[max(0, first - words) : first]),
            " ".join(texts[last + 1 : last + 1 + words]),
        )

    def context_at_caret(
        self, side: str, caret: Optional[Location], words: int = _CONTEXT_SHOWN_WORDS
    ) -> tuple[str, str]:
        """Same as `context_words` for an INSERTION POINT (a `DiffItem`
        caret): the text on either side of the point where the missing
        or added text belongs. The nearest word goes to whichever side
        of the caret it sits on."""
        if caret is None:
            return "", ""
        x0, top, x1, bottom = caret.bbox
        index = self.token_at(
            side, caret.page, (x0 + x1) / 2.0, (top + bottom) / 2.0, max_distance=None
        )
        if index is None:
            return "", ""
        location = self.location(side, index)
        # A word left of the caret ends the "before" text; one right of
        # it starts the "after" text.
        before_end = index + 1 if location is not None and location.bbox[2] <= x1 else index
        texts = self._texts[side]
        return (
            " ".join(texts[max(0, before_end - words) : before_end]),
            " ".join(texts[before_end : before_end + words]),
        )
