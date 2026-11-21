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

Lines are peeled from the page edges inward (up to three deep: a stamp
over a second stamp line over "Page N of M"). A line both documents
repeat is set aside in both -- each keeps its first copy, compared like
any other line -- and a line one document repeats is set aside from the
other too where it recurs there: removing it from one side only turned
the other side's copies into false "deleted" rows. The set-aside copies
are listed, one row per line (core/completeness.py), as formatting
extras.
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
# Running lines stacked at a page edge: stamp, stamp, "Page N of M".
_MAX_ROUNDS = 3
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


def _detected_lines(tokens: list[ComparisonToken]) -> list[tuple[str, list[list[int]], str]]:
    """Each running header/footer line found in ``tokens`` itself: (its
    text as first read, the token indices of every copy in page order,
    "top" or "bottom").

    Peeled from the page edges inward, up to `_MAX_ROUNDS` lines deep: a
    label stamped with "This label may not be the latest approved by
    FDA." over "For current labeling information, please visit ..." over
    "Page 11 of 45" carries three running lines above its text, and only
    the outermost was found -- the other two were read into the body and
    broke the comparison of the text around them on every page."""
    lines = _lines_by_page(tokens)
    if len(lines) < _MIN_PAGES:
        return []
    needed = max(_MIN_PAGES, _MIN_PAGE_SHARE * len(lines))
    remaining = {page: list(page_lines) for page, page_lines in lines.items()}
    found: list[tuple[str, list[list[int]], str]] = []
    for _round in range(_MAX_ROUNDS):
        # (position, key) -> [(page, token indices)] for each page's outermost line left
        seen: dict[tuple[str, str], list[tuple[int, list[int]]]] = defaultdict(list)
        for page, page_lines in sorted(remaining.items()):
            if not page_lines:
                continue
            seen[("top", _key(tokens, page_lines[0]))].append((page, page_lines[0]))
            if len(page_lines) > 1:
                seen[("bottom", _key(tokens, page_lines[-1]))].append((page, page_lines[-1]))
        peeled = False
        for (position, _text), occurrences in seen.items():
            if len({page for page, _ in occurrences}) < needed:
                continue
            if any(len(line) > _MAX_WORDS for _page, line in occurrences):
                continue
            first_line = occurrences[0][1]
            words = [tokens[k].text for k in sorted(first_line, key=lambda k: tokens[k].source_words[0].x0)]
            found.append((" ".join(words), [sorted(line) for _page, line in occurrences], position))
            for page, line in occurrences:
                remaining[page].remove(line)
            peeled = True
        if not peeled:
            break
    return found


def _copies_in(tokens: list[ComparisonToken], text: str) -> list[list[int]]:
    """Every whole line of ``tokens`` reading ``text`` (numbers ignored),
    as token indices, in page order. A whole line only: "on 27-04-2023 at
    12:48 PM" also ends the footer "Generated by admin-Mubashir on
    28-04-2023 at 05:07 PM", and cutting it out of those footers left
    their other halves unmatched on every page."""
    target = _DIGITS.sub("#", text)
    return [
        sorted(line)
        for _page, page_lines in sorted(_lines_by_page(tokens).items())
        for line in page_lines
        if _key(tokens, line) == target
    ]


def _furniture_lines(
    tokens: list[ComparisonToken], other: list[ComparisonToken] | None
) -> list[tuple[str, list[list[int]]]]:
    """Each running header/footer line: (its text as first read, the
    token indices of its repeats -- every copy after the first). See
    `_furniture_entries`."""
    return [(text, copies[1:]) for text, copies, _position in _furniture_entries(tokens, other)]


def _furniture_entries(
    tokens: list[ComparisonToken], other: list[ComparisonToken] | None
) -> list[tuple[str, list[list[int]], str]]:
    """Each running header/footer line of ``tokens``: (its text as first
    read, the token indices of every copy, "top" or "bottom").

    A line BOTH documents repeat is set aside in both: its copies fall
    wherever each document's pages break, and two versions of a label
    whose text was rewritten break their pages in different places -- the
    45 "Reference ID: 4198886" copies of one landed among different words
    than the 40 "Reference ID: 5405221" of the other, and split the text
    around every one of them. The first copy on each side is still
    compared. A line found only in the OTHER document is set aside here
    too when it recurs here (removing it from one side only turned the
    other side's copies into a dozen false "deleted" rows)."""
    own = _detected_lines(tokens)
    # Every whole-line copy counts, not only those found at a page edge: a
    # report's "S.No Type Source Destination Comments" heads pages 4-15,
    # but sits lower on page 3 under the report title -- left in the text
    # there, it was a copy too many against the other report.
    found = []
    for text, copies, position in own:
        detected = {tuple(copy) for copy in copies}
        extra = [copy for copy in _copies_in(tokens, text) if tuple(copy) not in detected]
        found.append((text, sorted(copies + extra, key=lambda copy: copy[0]), position))
    if other is not None:
        own_keys = {_DIGITS.sub("#", text) for text, _copies, _position in own}
        for text, _copies, position in _detected_lines(other):
            if _DIGITS.sub("#", text) in own_keys:
                continue
            here = _copies_in(tokens, text)
            if len(here) >= 2:
                first = sorted(here[0], key=lambda k: tokens[k].source_words[0].x0)
                found.append((" ".join(tokens[k].text for k in first), here, position))
    return found


def drop_repeated_furniture(
    tokens: list[ComparisonToken], other: list[ComparisonToken] | None = None
) -> tuple[list[ComparisonToken], list[str]]:
    """``tokens`` without the repeats of running headers/footers, and the
    furniture lines found (as their first occurrence reads). With
    ``other`` (the document it is compared with), see `_furniture_lines`.

    The first copy of each line stays, compared like any text -- read
    whole, at the top (or foot) of its page. On a two-column page a
    stamp across both columns was read half with each ("This label may
    not be the" in the left column, "latest approved by FDA." in the
    right), and each half split the text it landed in."""
    entries = _furniture_entries(tokens, other)
    drop = {k for _text, copies, _position in entries for line in copies[1:] for k in line}
    if not drop:
        return tokens, []
    kept = [k for k in range(len(tokens)) if k not in drop]
    order = _first_copies_in_place(tokens, kept, entries)
    return [tokens[k] for k in order], [text for text, _copies, _position in entries]


def _first_copies_in_place(
    tokens: list[ComparisonToken], kept: list[int], entries: list[tuple[str, list[list[int]], str]]
) -> list[int]:
    """``kept`` (token indices, reading order) with the first copy of each
    running line (``entries``, see `_furniture_entries`) moved, whole and
    left to right, to the start (a header) or end (a footer) of its page's
    text -- in both documents alike, so the two first copies line up."""
    heads: dict[int, list[list[int]]] = defaultdict(list)
    feet: dict[int, list[list[int]]] = defaultdict(list)
    moved: set[int] = set()
    kept_set = set(kept)
    for _text, copies, position in entries:
        first = [k for k in copies[0] if k in kept_set]
        if not first:
            continue
        first.sort(key=lambda k: tokens[k].source_words[0].x0)
        page = tokens[first[0]].source_words[0].page
        (heads if position == "top" else feet)[page].append(first)
        moved.update(first)
    if not moved:
        return kept

    def top(line: list[int]) -> float:
        return tokens[line[0]].source_words[0].top

    order: list[int] = []
    pages_done: set[int] = set()
    rest = [k for k in kept if k not in moved]
    for i, k in enumerate(rest):
        page = tokens[k].page
        if page not in pages_done:
            pages_done.add(page)
            for line in sorted(heads.pop(page, []), key=top):
                order.extend(line)
        order.append(k)
        next_page = tokens[rest[i + 1]].page if i + 1 < len(rest) else None
        if next_page != page:
            for line in sorted(feet.pop(page, []), key=top):
                order.extend(line)
    for line in [line for lines in (*heads.values(), *feet.values()) for line in lines]:
        order.extend(line)  # a page with nothing else on it
    return order


def furniture_repeats(
    tokens: list[ComparisonToken], other: list[ComparisonToken] | None = None
) -> list[tuple[str, list[list[ComparisonToken]]]]:
    """The repeats `drop_repeated_furniture` sets aside, as tokens: (line
    text, [the tokens of each copy after the first]). They are not compared
    one by one -- `core.completeness.furniture_rows` lists them, one row
    per line, so no word goes unaccounted for."""
    return [
        (text, [[tokens[k] for k in line] for line in repeats])
        for text, repeats in _furniture_lines(tokens, other)
        if repeats
    ]
