"""Running headers and footers (core/page_furniture.py).

A Word draft laid out over 34 pages repeats "Draft Labeling Text
1.14.1.3" at the top of every page and "Page N of 34" at the bottom.
Compared with a 2-page printed outsert that has neither, each repeat was
a separate "Inserted" row -- 31 of them -- and the leftovers were paired
with unrelated text on the other side ("COLORS: 1 REMARK : SUBSTRATE ..."
against "34 Draft Labeling").

`drop_repeated_furniture` removes the REPEATS of such a line and keeps
its first occurrence, which is still compared: a header that changed
between versions ("Version 3" -> "Version 4") is reported once instead
of once per page, never silently dropped.

A line counts as furniture only when it is the first or last line of a
page, it is SHORT (at most `_MAX_WORDS` words -- a running head, not a
paragraph line), and its text (numbers ignored, so "Page 17 of 34"
matches "Page 1 of 34") recurs in that position on at least `_MIN_PAGES`
pages and on `_MIN_PAGE_SHARE` of the document's pages. A test document
that repeats the same paragraph at the top of three of its five pages is
therefore left alone: those are full-width body lines on 60% of pages.

And only when the OTHER document does not repeat the same line too: two
reports that both carry "HARMONY ePROOF REPORT ... Created By Mubashir"
on every page line those copies up with each other, and removing them
from one side only (where the line happened to be detected) turned the
other side's copies into a dozen false "deleted" rows.
No Qt: standalone-testable.
"""

from __future__ import annotations

import re
from collections import defaultdict

from core.pdf_extractor import ComparisonToken

_MIN_PAGES = 3
_MIN_PAGE_SHARE = 0.8
_MAX_WORDS = 12
_LINE_TOLERANCE = 2.0
_DIGITS = re.compile(r"\d+")


def _lines_by_page(tokens: list[ComparisonToken]) -> dict[int, list[list[int]]]:
    """Token indices grouped into visual lines, per page, top to bottom."""
    by_page: dict[int, list[int]] = defaultdict(list)
    for index, token in enumerate(tokens):
        if token.source_words:
            by_page[token.source_words[0].page].append(index)
    lines: dict[int, list[list[int]]] = {}
    for page, indices in by_page.items():
        indices.sort(key=lambda k: (tokens[k].source_words[0].top, tokens[k].source_words[0].x0))
        page_lines: list[list[int]] = []
        for k in indices:
            top = tokens[k].source_words[0].top
            if page_lines and abs(tokens[page_lines[-1][0]].source_words[0].top - top) <= _LINE_TOLERANCE:
                page_lines[-1].append(k)
            else:
                page_lines.append([k])
        lines[page] = page_lines
    return lines


def _key(tokens: list[ComparisonToken], line: list[int]) -> str:
    ordered = sorted(line, key=lambda k: tokens[k].source_words[0].x0)
    return _DIGITS.sub("#", " ".join(tokens[k].text for k in ordered))


def _occurrences(tokens: list[ComparisonToken], words: list[str]) -> int:
    """How many times ``words`` (numbers ignored) occur in ``tokens``."""
    if not words:
        return 0
    keys = [_DIGITS.sub("#", t.text) for t in tokens]
    target = [_DIGITS.sub("#", w) for w in words]
    n = len(target)
    return sum(1 for i in range(len(keys) - n + 1) if keys[i:i + n] == target)


def drop_repeated_furniture(
    tokens: list[ComparisonToken], other: list[ComparisonToken] | None = None
) -> tuple[list[ComparisonToken], list[str]]:
    """``tokens`` without the repeats of running headers/footers, and the
    furniture lines found (as their first occurrence reads). With
    ``other`` (the document it is compared with), a line the other
    document also repeats is kept: the two sets of copies match up."""
    lines = _lines_by_page(tokens)
    if len(lines) < _MIN_PAGES:
        return tokens, []
    # (position, key) -> [(page, token indices)] for each page's first and last line
    seen: dict[tuple[str, str], list[tuple[int, list[int]]]] = defaultdict(list)
    for page, page_lines in sorted(lines.items()):
        if not page_lines:
            continue
        seen[("top", _key(tokens, page_lines[0]))].append((page, page_lines[0]))
        if len(page_lines) > 1:
            seen[("bottom", _key(tokens, page_lines[-1]))].append((page, page_lines[-1]))
    needed = max(_MIN_PAGES, _MIN_PAGE_SHARE * len(lines))
    drop: set[int] = set()
    found: list[str] = []
    for (_position, _text), occurrences in seen.items():
        if len({page for page, _ in occurrences}) < needed:
            continue
        if any(len(line) > _MAX_WORDS for _page, line in occurrences):
            continue
        first_page, first_line = occurrences[0]
        words = [tokens[k].text for k in sorted(first_line, key=lambda k: tokens[k].source_words[0].x0)]
        if other is not None and _occurrences(other, words) >= 2:
            continue
        found.append(" ".join(words))
        for _page, line in occurrences[1:]:
            drop.update(line)
    if not drop:
        return tokens, []
    return [t for k, t in enumerate(tokens) if k not in drop], found
