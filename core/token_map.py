"""Word-level correspondence between the two compared documents.

Built once per comparison. Answers "the user clicked this word in one
document -- where is the same text in the other?", and never guesses:

  1. A word the comparison matched, the match confirmed (`confirmed`:
     shown green), maps to the word it was matched with -- exactly the
     comparison's pairing, so what a reviewer clicks is what was compared.
  2. Otherwise there is no counterpart: the word is in a difference row,
     and the UI shows that row instead of jumping anywhere else.

Positions come from the comparison, not page coordinates, so this stays
exact when the documents are laid out differently (different page
sizes, columns, a section moved to another page).

No Qt imports -- standalone-testable.
"""

from __future__ import annotations

import re
from typing import Optional

from core.diff_engine import DiffItem, Location, TokenMatches, TrustedPair, _is_whitespace_artifact, _location_of
from core.pdf_extractor import ComparisonToken

APPROVED = "approved"
SUSPECT = "suspect"

EXACT = "exact"  # the diff engine matched this word

# A click farther than this (PDF points) from every word hits nothing.
_MAX_HIT_DISTANCE = 40.0
# Matched runs shorter than this can be coincidences (a lone "and"
# paired across unrelated text), so they need the phrase check too.
_MIN_RELIABLE_RUN = 3
# A shorter matched run still counts as confirmed when it sits in a small
# gap -- at most this many words on EACH side -- between reliable runs, in
# the same order in both documents: "take one tablet twice daily" / "take
# ONE tablet daily", where "tablet" is the same word in the same place
# between two one-word edits. A short run inside a long rewritten stretch
# ("and" shared by two different paragraphs) is a coincidence, and is not.
_CONFIRM_WINDOW = 8
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
        # approved index -> suspect index of each `TrustedPair` (a short row
        # found moved whole): confirmed though no text around it is matched.
        self._trusted: dict[int, int] = {}
        for pair in matches:
            token_a, token_b = pair
            i = index[APPROVED].get(id(token_a))
            j = index[SUSPECT].get(id(token_b))
            if i is not None and j is not None:
                self._partner[APPROVED][i] = j
                self._partner[SUSPECT][j] = i
                if isinstance(pair, TrustedPair):
                    self._trusted[i] = j
        self._pair_split_words()
        self._run_length = {side: self._run_lengths(side) for side in self._tokens}
        self._confirmed = self._confirmations()


        # side -> page -> [(token index, (x0, top, x1, bottom)) per source word]
        self._by_page: dict[str, dict[int, list[tuple[int, tuple]]]] = {}
        for side, tokens in self._tokens.items():
            pages: dict[int, list[tuple[int, tuple]]] = {}
            for i, token in enumerate(tokens):
                for word in token.source_words:
                    pages.setdefault(word.page, []).append((i, word.bbox))
            self._by_page[side] = pages

    def _pair_split_words(self) -> None:
        """Pair the words one text layer splits where the other does not.

        A PDF can draw one word in pieces ("القل ب." against "القلب.",
        "أبديل- أتش" against "أبديل-أتش"): the same letters, a space in
        another place. The comparison drops such a span as a text-layer
        artifact -- it is not a difference -- but the word stream had no
        pair for it, so it was neither in the list nor shown as matched,
        and a click on it found nothing. Each unpaired stretch between two
        paired words is paired letter by letter with the unpaired stretch
        between their partners, when the two hold the same letters and the
        comparison drops the change. A space lost or added next to a digit
        ("100 mg" / "100mg") is a reported difference, and stays unpaired.
        """
        mine, theirs = self._partner[APPROVED], self._partner[SUSPECT]
        n_a, n_b = len(mine), len(theirs)
        i = 0
        while i < n_a:
            if mine[i] >= 0:
                i += 1
                continue
            start = i
            while i < n_a and mine[i] < 0:
                i += 1
            # Unpaired approved words [start, i); their neighbours' partners
            # bracket the suspect stretch they stand for.
            before = mine[start - 1] if start > 0 else -1
            after = mine[i] if i < n_a else n_b
            if start > 0 and before < 0 or after < 0 or after - before < 2:
                continue
            gap = range(before + 1, after)
            if any(theirs[j] >= 0 for j in gap):
                continue
            run_a, run_b = list(range(start, i)), list(gap)
            if self._is_split_word(run_a, run_b):
                self._pair_by_letters(run_a, run_b)
                continue
            # A real change in the middle ("(5.2)] . 2.3 Concomitant"
            # against "(5.2)]. 2.4 Recommendations"): the same letters at
            # its start or end are still paired.
            for reverse in (False, True):
                ends = self._common_letters(run_a, run_b, reverse)
                if ends is not None:
                    part_a, part_b = ends
                    self._pair_by_letters(part_a, part_b)
                    run_a = [k for k in run_a if k not in part_a]
                    run_b = [k for k in run_b if k not in part_b]

    def _is_split_word(self, run_a: list[int], run_b: list[int]) -> bool:
        """Same letters, spaces elsewhere, and dropped by the comparison."""
        if not run_a or not run_b:
            return False
        text_a = " ".join(self._texts[APPROVED][k] for k in run_a)
        text_b = " ".join(self._texts[SUSPECT][k] for k in run_b)
        return _is_whitespace_artifact(DiffItem(0, "text_changed", "minor", text_a, text_b, None, None))

    def _common_letters(
        self, run_a: list[int], run_b: list[int], reverse: bool
    ) -> Optional[tuple[list[int], list[int]]]:
        """The longest start (or end) of the two stretches that holds the
        same letters, ending on a word boundary on both sides and dropped
        by the comparison as spacing -- or None."""
        seq_a, seq_b = (run_a[::-1], run_b[::-1]) if reverse else (run_a, run_b)

        def letters(side: str, index: int) -> str:
            return re.sub(r"\s+", "", self._texts[side][index])

        a = b = 0
        text_a = text_b = ""
        best = None
        while True:
            if text_a and text_a == text_b:
                part_a, part_b = seq_a[:a], seq_b[:b]
                if self._is_split_word(sorted(part_a), sorted(part_b)):
                    best = (sorted(part_a), sorted(part_b))
            if len(text_a) <= len(text_b) and a < len(seq_a):
                piece = letters(APPROVED, seq_a[a])
                text_a = piece + text_a if reverse else text_a + piece
                a += 1
            elif b < len(seq_b):
                piece = letters(SUSPECT, seq_b[b])
                text_b = piece + text_b if reverse else text_b + piece
                b += 1
            else:
                break
            shorter, longer = sorted((text_a, text_b), key=len)
            if not (longer.endswith(shorter) if reverse else longer.startswith(shorter)):
                break
        return best

    def _pair_by_letters(self, run_a: list[int], run_b: list[int]) -> None:
        """Each word of one stretch paired with the first word of the
        other that shares letters with it (same letters, other spaces)."""
        def spans(side: str, run: list[int]) -> list[tuple[int, int, int]]:
            out, position = [], 0
            for index in run:
                size = len(re.sub(r"\s+", "", self._texts[side][index]))
                out.append((position, position + max(size, 1), index))
                position += size
            return out

        spans_a, spans_b = spans(APPROVED, run_a), spans(SUSPECT, run_b)
        for side, own, other in ((APPROVED, spans_a, spans_b), (SUSPECT, spans_b, spans_a)):
            for lo, hi, index in own:
                partner = next((k for o_lo, o_hi, k in other if o_lo < hi and lo < o_hi), None)
                if partner is not None:
                    self._partner[side][index] = partner

    # ------------------------------------------------------------------
    # Re-pairing (a reviewer's area rematch, core/rematch.rematch_areas)
    # ------------------------------------------------------------------

    def partners(self) -> dict[str, list[int]]:
        """Per side, each token's partner index in the other document,
        or -1. Read-only: change it with `rebind`."""
        return self._partner

    def confirmed(self, side: str, index: int) -> bool:
        """Whether word ``index`` is matched AND the match is confirmed: it
        lies in a run of at least `_MIN_RELIABLE_RUN` words matched in
        sequence. A lone pair between changes ("and" in two different
        sentences) can be a coincidence: it is not shown as matched, and
        the words are listed as a difference instead."""
        return self._confirmed[side][index]

    def trust(self, pairs: list[tuple[int, int]]) -> None:
        """Confirm the given (approved index, suspect index) pairs -- both
        already partners -- whatever the text around them: words that only
        moved, found whole and alike on both sides (see
        `core.completeness.unaccounted_rows`)."""
        for i, j in pairs:
            if self._partner[APPROVED][i] == j:
                self._trusted[i] = j
                self._confirmed[APPROVED][i] = True
                self._confirmed[SUSPECT][j] = True

    def _confirmations(self) -> dict[str, list[bool]]:
        """`confirmed` for every word, both documents. Decided on the
        Original's side; a Suspect word follows its partner, so a pair is
        confirmed (or not) on both sides at once."""
        partners, runs = self._partner[APPROVED], self._run_length[APPROVED]
        n_a, n_b = len(partners), len(self._partner[SUSPECT])

        def reliable(k: int) -> bool:
            return partners[k] >= 0 and runs[k] >= _MIN_RELIABLE_RUN

        def anchored(i: int) -> bool:
            lo = i - 1
            while lo >= 0 and not reliable(lo):
                if i - lo > _CONFIRM_WINDOW:
                    return False
                lo -= 1
            hi = i + 1
            while hi < n_a and not reliable(hi):
                if hi - i > _CONFIRM_WINDOW:
                    return False
                hi += 1
            lo_other = partners[lo] if lo >= 0 else -1
            hi_other = partners[hi] if hi < n_a else n_b
            if hi - lo - 1 > _CONFIRM_WINDOW or hi_other - lo_other - 1 > _CONFIRM_WINDOW:
                return False
            return lo_other < partners[i] < hi_other

        approved = [
            partners[i] >= 0
            and (runs[i] >= _MIN_RELIABLE_RUN or self._trusted.get(i) == partners[i] or anchored(i))
            for i in range(n_a)
        ]
        suspect = [
            (j_partner := self._partner[SUSPECT][j]) >= 0 and approved[j_partner] for j in range(n_b)
        ]
        return {APPROVED: approved, SUSPECT: suspect}

    def matched_boxes(self, side: str) -> dict[int, list[tuple[float, float, float, float]]]:
        """Where the words of ``side`` whose match is confirmed (see
        `confirmed`) are, {page: [box]} in PDF points -- neighbouring
        matched words on one line joined into one box (a few thousand
        boxes for a long label, not one per word)."""
        pages: dict[int, list[list[float]]] = {}
        last: Optional[tuple[int, list[float]]] = None
        for index, token in enumerate(self._tokens[side]):
            if not self.confirmed(side, index):
                last = None  # a gap: the next match starts a new box
                continue
            for word in token.source_words:
                x0, top, x1, bottom = word.bbox
                if last is not None:
                    page, box = last
                    height = min(box[3] - box[1], bottom - top)
                    same_line = (
                        page == word.page
                        and min(box[3], bottom) - max(box[1], top) > 0.5 * height
                        and -height <= x0 - box[2] <= 1.5 * height
                    )
                    if same_line:
                        box[1], box[2], box[3] = min(box[1], top), max(box[2], x1), max(box[3], bottom)
                        continue
                box = [x0, top, x1, bottom]
                pages.setdefault(word.page, []).append(box)
                last = (word.page, box)
        return {page: [tuple(box) for box in boxes] for page, boxes in pages.items()}

    def snapshot(self) -> dict[str, list[int]]:
        return {side: list(partners) for side, partners in self._partner.items()}

    def restore(self, snapshot: dict[str, list[int]]) -> None:
        self._partner = {side: list(partners) for side, partners in snapshot.items()}
        self._run_length = {side: self._run_lengths(side) for side in self._tokens}
        self._confirmed = self._confirmations()

    def rebind(
        self,
        approved_indices: list[int],
        suspect_indices: list[int],
        pairs: list[tuple[int, int]],
    ) -> None:
        """Make ``pairs`` (approved index, suspect index) the only
        partners of the given tokens: whatever they were paired with
        before -- inside or outside the given tokens -- is let go."""
        for side, indices in ((APPROVED, approved_indices), (SUSPECT, suspect_indices)):
            mine, theirs = self._partner[side], self._partner[other_side(side)]
            for index in indices:
                partner = mine[index]
                if partner >= 0 and theirs[partner] == index:
                    theirs[partner] = -1
                mine[index] = -1
        for i, j in pairs:
            self._partner[APPROVED][i] = j
            self._partner[SUSPECT][j] = i
        self._run_length = {side: self._run_lengths(side) for side in self._tokens}
        self._confirmed = self._confirmations()

    def _run_lengths(self, side: str) -> list[int]:
        """For each word, the length of the in-sequence matched run it
        belongs to (0 if unmatched)."""
        partners = self._partner[side]
        theirs = self._partner[other_side(side)]

        def continues(before: int, after: int) -> bool:
            """``after`` follows ``before`` in the other document too --
            the next word, or the same word / the words between belonging
            to the two (a word one layer splits: "القل ب." / "القلب.")."""
            a, b = partners[before], partners[after]
            if b < 0 or b < a:
                return False
            return b - a <= 1 or all(theirs[k] in (before, after) for k in range(a + 1, b))

        lengths = [0] * len(partners)
        i = 0
        while i < len(partners):
            if partners[i] < 0:
                i += 1
                continue
            end = i + 1
            while end < len(partners) and continues(end - 1, end):
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
        """The word the comparison matched ``index`` with: (its index,
        EXACT) when the match is confirmed -- the very pairing the word is
        shown green for -- else (None, None).

        Nothing else: a word was once also looked up by the five words
        around it, anywhere in the other document, and a click on a word
        the comparison had NOT matched jumped to the same phrase in some
        other paragraph -- a reviewer checking which word was matched to
        which saw a pairing the comparison never made. A word without a
        confirmed match is in a difference row; the viewer shows that."""
        partner = self._partner[side][index]
        if partner >= 0 and self.confirmed(side, index):
            return partner, EXACT
        return None, None

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

    def tokens(self, side: str) -> list[ComparisonToken]:
        """That document's full token stream, as the comparison read it
        (what `core.rematch` searches)."""
        return self._tokens[side]

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
