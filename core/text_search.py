"""Find words and sentences in a document, the way a viewer's Find bar does.

Qt-free so it can run on a background thread and be tested on its own.
A document's words (reading order, as the page lists them) are joined
with single spaces into one searchable string, so a phrase is found
across line and page breaks. Each match comes back as per-line boxes in
PDF points, the same coordinates the viewer already uses for diff
highlights.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import fitz  # PyMuPDF

# Characters that look alike on a page but differ in the text layer:
# a search for "don't" should find "don’t", "-" should find "–".
_FOLD = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    " ": " ", "­": "",
})


# Rough glyph widths relative to a letter, for boxing part of a word
# ("disorder" inside "disorder,") without the page's character boxes.
_NARROW = set(".,;:'\"!|()[]{}/-ijlrtfI1 ")


def _width_at(text: str, pos: int) -> float:
    """Share of ``text``'s width taken by its first ``pos`` characters."""
    weights = [0.5 if ch in _NARROW else 1.0 for ch in text]
    total = sum(weights)
    return sum(weights[:pos]) / total if total else 0.0


def _fold(text: str) -> str:
    """Text as searched: ligatures and look-alike punctuation folded."""
    return unicodedata.normalize("NFKC", text).translate(_FOLD)


@dataclass(frozen=True)
class SearchWord:
    page: int  # 1-indexed
    x0: float
    top: float
    x1: float
    bottom: float
    text: str
    # A rotated page's words are kept as the page's text runs (unrotated,
    # so part of a word is cut along the line) with the page's rotation
    # matrix as 6 numbers, applied to the boxes a match comes back with.
    rotation: tuple | None = None


@dataclass
class SearchMatch:
    page: int  # first page the match touches
    # (page, boxes) per page touched; each box is one line's span in PDF points.
    pages: list[tuple[int, list[tuple[float, float, float, float]]]] = field(default_factory=list)

    def iter_pages(self):
        return iter(self.pages)


class SearchIndex:
    """A document's words joined into one string, with each word's span."""

    def __init__(self, words: list[SearchWord]) -> None:
        self.words: list[SearchWord] = []
        self._starts: list[int] = []
        parts: list[str] = []
        pos = 0
        for word in words:
            text = " ".join(_fold(word.text).split())
            if not text:
                continue
            if parts:
                parts.append(" ")
                pos += 1
            self.words.append(word)
            self._starts.append(pos)
            parts.append(text)
            pos += len(text)
        self.text = "".join(parts)

    def find_all(self, query: str, *, match_case: bool = False, whole_word: bool = False) -> list[SearchMatch]:
        """Every place ``query`` occurs, in document order. Runs of
        whitespace in the query match a single break between words."""
        query = " ".join(_fold(query).split())
        if not query or not self.text:
            return []
        flags = 0 if match_case else re.IGNORECASE
        pattern = re.escape(query).replace(r"\ ", " ")
        if whole_word:
            pattern = rf"(?<!\w){pattern}(?!\w)"
        return [self._match_for(m.start(), m.end()) for m in re.finditer(pattern, self.text, flags)]

    def _word_at(self, pos: int) -> int:
        lo, hi = 0, len(self._starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _match_for(self, start: int, end: int) -> SearchMatch:
        first, last = self._word_at(start), self._word_at(end - 1)
        lines: list[tuple[int, list[float], tuple | None]] = []  # (page, [x0, top, x1, bottom], rotation)
        for i in range(first, last + 1):
            word = self.words[i]
            x0, x1 = word.x0, word.x1
            folded = " ".join(_fold(word.text).split())
            # Only part of a word matched: box that share of it.
            lo = max(start - self._starts[i], 0)
            hi = min(end - self._starts[i], len(folded))
            if lo > 0 or hi < len(folded):
                width = word.x1 - word.x0
                x0 = word.x0 + width * _width_at(folded, lo)
                x1 = word.x0 + width * _width_at(folded, hi)
            if lines:
                page, box, _rotation = lines[-1]
                same_line = (
                    page == word.page
                    and x0 >= box[0] - 1.0
                    and min(box[3], word.bottom) - max(box[1], word.top)
                    > 0.5 * min(box[3] - box[1], word.bottom - word.top)
                )
                if same_line:
                    box[1], box[2], box[3] = min(box[1], word.top), max(box[2], x1), max(box[3], word.bottom)
                    continue
            lines.append((word.page, [x0, word.top, x1, word.bottom], word.rotation))
        match = SearchMatch(page=lines[0][0])
        for page, box, rotation in lines:
            if rotation is not None:
                box = tuple(fitz.Rect(box) * fitz.Matrix(*rotation))
            if not match.pages or match.pages[-1][0] != page:
                match.pages.append((page, []))
            match.pages[-1][1].append(tuple(box))
        return match


def pdf_search_words(path: str) -> list[SearchWord]:
    """Every word of a PDF in the page's own reading order, with boxes in
    the coordinates the page is drawn in (crop box origin, rotation
    applied)."""
    words: list[SearchWord] = []
    with fitz.open(path) as doc:
        for index, page in enumerate(doc):
            rotation = tuple(page.rotation_matrix) if page.rotation else None
            for x0, y0, x1, y1, text, *_ in page.get_text("words", sort=False):
                words.append(SearchWord(index + 1, x0, y0, x1, y1, text, rotation))
    return words


def build_search_index(path: str, kind: str) -> SearchIndex:
    """Index the document shown in a viewer: a PDF's own text layer, or
    for a synthesized preview the same words the preview was drawn from."""
    if kind == "pdf":
        return SearchIndex(pdf_search_words(path))
    from core.document_extractor import extract_synthesized_words

    return SearchIndex([
        SearchWord(w.page, w.x0, w.top, w.x1, w.bottom, w.text) for w in extract_synthesized_words(path)
    ])
