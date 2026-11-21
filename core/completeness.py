"""Every word is accounted for (core/completeness.py).

The rule this module enforces, after every comparison: each word of both
documents is EITHER matched to the other document (it is tinted green,
see `core.token_map.TokenMap.matched_boxes`) OR inside a row of the
deviation list. Never neither.

The comparison mostly gets there by itself. This is the last check, so
that no word can fall between the two whatever the cause -- a span the
comparison drops as text-layer noise that the word pairing could not pair,
a running header's repeats set aside so they are not reported once per
page, an alignment that jumps out of sequence:

* `furniture_rows`: a running header/footer the other document does not
  have is compared on its first page; its copies on the other pages become
  ONE row, highlighting every copy ("Draft Labeling Text 1.14.1.3 -- on 67
  more pages").
* `unaccounted_rows`: any word still neither matched nor in a row gets a
  row of its own, consecutive words together, as "Not matched".

No Qt: standalone-testable.
"""

from __future__ import annotations

from collections import Counter
from typing import Optional

from core.diff_engine import FURNITURE_NOTE, DiffItem, _caret_location, _classify_severity, _location_of
from core.pdf_extractor import ComparisonToken
from core.token_map import APPROVED, SUSPECT, TokenMap

# Marks the text of a row this module added, so the UI and the report
# can say why it is there (FURNITURE_NOTE: core/diff_engine.py).
UNMATCHED_NOTE = "not matched"


def furniture_rows(
    repeats: list[tuple[str, list[list[ComparisonToken]]]], side: str, start_index: int
) -> list[DiffItem]:
    """One row per running header/footer whose repeats were set aside
    (see `core.page_furniture.furniture_repeats`): every copy after the
    first, on every page, in one row. ``side`` is the document they are
    in. Rated minor: the first copy is compared (and reported, if it is not
    in the other document) like any other line."""
    rows: list[DiffItem] = []
    for text, copies in repeats:
        tokens = [token for copy in copies for token in copy]
        if not tokens:
            continue
        pages = sorted({token.source_words[0].page for token in tokens if token.source_words})
        label = f"{text} ({FURNITURE_NOTE}: {len(copies)} more cop{'y' if len(copies) == 1 else 'ies'}, " \
                f"page{'s' if len(pages) > 1 else ''} {_page_list(pages)})"
        location = _location_of(tokens)
        rows.append(DiffItem(
            index=start_index + len(rows),
            kind="removed_in_suspect" if side == APPROVED else "added_in_suspect",
            severity="minor",
            approved_text=label if side == APPROVED else "",
            suspect_text=label if side == SUSPECT else "",
            approved_location=location if side == APPROVED else None,
            suspect_location=location if side == SUSPECT else None,
            approved_span=tokens if side == APPROVED else None,
            suspect_span=tokens if side == SUSPECT else None,
        ))
    return rows


def place_insertion_points(items: list[DiffItem], token_map: TokenMap) -> None:
    """Give every row present on one side only an insertion point in the
    other document, when it has none: right after the partner of the
    nearest CONFIRMED match before it (or, at the very start, right before
    the partner of the nearest one after it).

    A row without one was marked by drawing its own boxes at the same page
    coordinates on the other document, which in a different layout lands on
    unrelated text: the contents line "2.2 Recommended Dosage in Pediatric
    Patients", missing from a Word draft, was marked in the middle of the
    draft's Hearing Loss paragraph on page 4 instead of after "2.1
    Recommended Dosage in Adults" in its contents list. Rows the comparison
    builds for a whole section missing on the other side, rows rebuilt after
    moved text was paired, and repeated-header rows get theirs here."""
    for side, other in ((APPROVED, SUSPECT), (SUSPECT, APPROVED)):
        tokens, other_tokens = token_map.tokens(side), token_map.tokens(other)
        if not tokens or not other_tokens:
            continue
        position = {id(t): i for i, t in enumerate(tokens)}
        partners = token_map.partners()[side]
        for item in items:
            own_text = item.approved_text if side == APPROVED else item.suspect_text
            other_text = item.suspect_text if side == APPROVED else item.approved_text
            caret = item.suspect_caret if side == APPROVED else item.approved_caret
            if not own_text or other_text or caret is not None:
                continue
            span = item.approved_span if side == APPROVED else item.suspect_span
            bounds = _span_bounds(span, position, tokens)
            if bounds is None:
                continue
            first, last = bounds
            for k in range(first - 1, -1, -1):
                if token_map.confirmed(side, k):
                    caret = _caret_location(other_tokens, partners[k] + 1, partners[k] + 1)
                    break
            if caret is None:
                for k in range(last + 1, len(tokens)):
                    if token_map.confirmed(side, k):
                        caret = _caret_location(other_tokens, partners[k], partners[k])
                        break
            if side == APPROVED:
                item.suspect_caret = caret
            else:
                item.approved_caret = caret


def _span_bounds(
    span: Optional[list], position: dict[int, int], tokens: list[ComparisonToken]
) -> Optional[tuple[int, int]]:
    """The first and last index of ``span``'s words in ``tokens``. Words
    set aside before the comparison (a running header's repeats) are not
    there: they stand just before the first word that follows them on the
    page (as (index, index - 1), an empty range at that point)."""
    if not span:
        return None
    found = [position[id(t)] for t in span if id(t) in position]
    if found:
        return min(found), max(found)
    word = next((t.source_words[0] for t in span if t.source_words), None)
    if word is None:
        return None
    for index, token in enumerate(tokens):
        if not token.source_words:
            continue
        w = token.source_words[0]
        if (w.page, w.top) >= (word.page, word.top):
            return index, index - 1
    return len(tokens), len(tokens) - 1


def unaccounted_rows(
    items: list[DiffItem], token_map: TokenMap, start_index: int
) -> list[DiffItem]:
    """Every run of words that is neither matched (confirmed) nor inside
    any row of ``items`` -- on both documents -- joined to the row next to
    it on its side (``items`` is changed), or given a row of its own.

    Such a run is mostly a few common words two rewritten passages share
    ("heart failure" in "if you have severe heart failure" / "if you have
    ever had heart failure or have problems"), paired by the comparison
    but not confirmed by the text around them. Listed apart as an
    "Unconfirmed match", they cut each rewritten passage into scraps of a
    change and the same words beside it; joined to the change they sit in,
    the passage reads as the one change it is."""
    rows: list[DiffItem] = []
    partners = token_map.partners()
    tokens_a, tokens_b = token_map.tokens(APPROVED), token_map.tokens(SUSPECT)
    covered_by_side = {APPROVED: _covered(items, tokens_a, APPROVED), SUSPECT: _covered(items, tokens_b, SUSPECT)}
    position = {side: {id(t): k for k, t in enumerate(token_map.tokens(side))} for side in (APPROVED, SUSPECT)}
    # (row before, row after) of every run joined to the row before it while
    # a row followed it too: once both sides are joined they may touch.
    bridged: list[tuple[DiffItem, DiffItem]] = []

    for side in (APPROVED, SUSPECT):
        tokens = token_map.tokens(side)
        other_tokens = token_map.tokens(SUSPECT if side == APPROVED else APPROVED)
        covered = covered_by_side[side]
        mine = partners[side]
        run: list[int] = []
        runs: list[list[int]] = []
        for index in range(len(tokens)):
            if not token_map.confirmed(side, index) and index not in covered:
                if run and index != run[-1] + 1:
                    runs.append(run)
                    run = []
                run.append(index)
        if run:
            runs.append(run)
        row_at = _rows_by_token(items, side, position[side])
        other_side = SUSPECT if side == APPROVED else APPROVED
        row_at_other = _rows_by_token(items, other_side, position[other_side])
        for run in runs:
            run_tokens = [tokens[k] for k in run]
            neighbour = row_at.get(run[0] - 1) or row_at.get(run[-1] + 1)
            moved = None if neighbour is not None else _moved_alike(run, side, token_map, row_at_other)
            if moved is not None:
                token_map.trust(moved)
                continue
            if neighbour is not None:
                _extend_row(neighbour, side, run_tokens, position[side])
                for k in run:
                    row_at[k] = neighbour
                following = row_at.get(run[-1] + 1)
                if following is not None and following is not neighbour:
                    bridged.append((neighbour, following))
                continue
            text = " ".join(t.text for t in run_tokens)
            # Where it would be in the other document: after the partner
            # of the nearest CONFIRMED match before it (as
            # `place_insertion_points`) -- a coincidental one-word pair put
            # a "(5.1)] ." deleted on page 22 of the Original on page 6 of
            # the Suspect.
            anchor = next(
                (mine[k] for k in range(run[0] - 1, -1, -1) if mine[k] >= 0 and token_map.confirmed(side, k)), -1
            )
            caret = _caret_location(other_tokens, anchor + 1, anchor + 1) if other_tokens else None
            location = _location_of(run_tokens)
            severity = _classify_severity("delete" if side == APPROVED else "insert",
                                          run_tokens if side == APPROVED else [],
                                          run_tokens if side == SUSPECT else [])
            rows.append(DiffItem(
                index=start_index + len(rows),
                kind="removed_in_suspect" if side == APPROVED else "added_in_suspect",
                severity=severity,
                approved_text=text if side == APPROVED else "",
                suspect_text=text if side == SUSPECT else "",
                approved_location=location if side == APPROVED else None,
                suspect_location=location if side == SUSPECT else None,
                approved_caret=None if side == APPROVED else caret,
                suspect_caret=caret if side == APPROVED else None,
                approved_span=run_tokens if side == APPROVED else None,
                suspect_span=run_tokens if side == SUSPECT else None,
            ))
    _merge_bridged(items, bridged, position)
    return rows


def _moved_alike(
    run: list[int], side: str, token_map: TokenMap, row_at_other: dict[int, DiffItem]
) -> Optional[list[tuple[int, int]]]:
    """The (approved, suspect) pairs of ``run`` when it is text that only
    moved: every word paired, in order, with the same word on the other
    side, no change beside it there either, and a word of 4+ letters in it
    (a lone "and" or "(%)" could be anything). A Contents entry the two
    layouts read in another order -- "2.1 Schizophrenia", "5.7
    Hyperprolactinemia" -- is matched, not a deletion and an insertion.
    None otherwise."""
    partners = token_map.partners()[side]
    theirs = [partners[k] for k in run]
    if any(j < 0 for j in theirs) or any(b != a + 1 for a, b in zip(theirs, theirs[1:])):
        return None
    if any(j in row_at_other for j in theirs) or theirs[0] - 1 in row_at_other or theirs[-1] + 1 in row_at_other:
        return None
    mine_text = [token_map.tokens(side)[k].text for k in run]
    other = SUSPECT if side == APPROVED else APPROVED
    if mine_text != [token_map.tokens(other)[j].text for j in theirs]:
        return None
    if not any(sum(ch.isalpha() for ch in text) >= 4 for text in mine_text):
        return None
    return [(k, j) if side == APPROVED else (j, k) for k, j in zip(run, theirs)]


def _rows_by_token(items: list[DiffItem], side: str, position: dict[int, int]) -> dict[int, DiffItem]:
    """Token index -> the row of ``items`` holding that word on ``side``
    (repeated-header rows aside: their words are not in the comparison)."""
    at: dict[int, DiffItem] = {}
    for item in items:
        text = item.approved_text if side == APPROVED else item.suspect_text
        span = item.approved_span if side == APPROVED else item.suspect_span
        if not span or FURNITURE_NOTE in text:
            continue
        for token in span:
            k = position.get(id(token))
            if k is not None:
                at.setdefault(k, item)
    return at


def _merge_bridged(
    items: list[DiffItem], bridged: list[tuple[DiffItem, DiffItem]], position: dict[str, dict[int, int]]
) -> None:
    """Merge each (row, next row) that the joined words leave touching on
    both documents -- one change, "Patients with severe heart failure must
    stop treatment" / "Doctors should check whether heart failure develops
    during long courses", no longer two rows cut at the shared words. The
    later row leaves ``items``."""
    merged_into: dict[int, DiffItem] = {}
    for first, second in bridged:
        while id(first) in merged_into:
            first = merged_into[id(first)]
        if first is second or not any(item is second for item in items):
            continue
        touching = True
        for side in (APPROVED, SUSPECT):
            span_1 = first.approved_span if side == APPROVED else first.suspect_span
            span_2 = second.approved_span if side == APPROVED else second.suspect_span
            if span_1 and span_2:
                end = max(position[side].get(id(t), -2) for t in span_1)
                start = min(position[side].get(id(t), -2) for t in span_2)
                touching = touching and start == end + 1
        if not touching:
            continue
        for side in (APPROVED, SUSPECT):
            span_2 = second.approved_span if side == APPROVED else second.suspect_span
            if span_2:
                _extend_row(first, side, list(span_2), position[side])
        if first.approved_span and first.suspect_span:
            first.kind = "text_changed"
        del items[next(k for k, item in enumerate(items) if item is second)]
        merged_into[id(second)] = first


def _extend_row(item: DiffItem, side: str, run: list[ComparisonToken], position: dict[int, int]) -> None:
    """``item`` with ``run`` (words beside it on ``side``) taken into it:
    its text, location and severity rebuilt."""
    span = (item.approved_span if side == APPROVED else item.suspect_span) or []
    merged = sorted(span + run, key=lambda t: position.get(id(t), -1))
    text = " ".join(t.text for t in merged)
    if side == APPROVED:
        item.approved_span, item.approved_text, item.approved_location = merged, text, _location_of(merged)
    else:
        item.suspect_span, item.suspect_text, item.suspect_location = merged, text, _location_of(merged)
    approved, suspect = item.approved_span or [], item.suspect_span or []
    tag = "replace" if approved and suspect else "delete" if approved else "insert"
    item.severity = _classify_severity(tag, approved, suspect)


def _covered(items: list[DiffItem], tokens: list[ComparisonToken], side: str) -> set[int]:
    """Indices of ``tokens`` inside a row of ``items``: the word sits in
    one of the row's line boxes on its page AND is one of the row's words
    (so a different word that happens to share a box is not counted).

    A row that carries its words (its span) covers exactly those, found
    by identity -- the box search is for a row without them. Searched for
    every row, it tested each of the 9,000 words on a 2-page outsert
    sheet against each of 700 rows' boxes: 27 million tests, 50s."""
    index_of = {id(token): index for index, token in enumerate(tokens)}
    covered: set[int] = set()
    searched: list[DiffItem] = []
    for item in items:
        span = item.approved_span if side == APPROVED else item.suspect_span
        found = [index_of[id(token)] for token in span or () if id(token) in index_of]
        if found:
            covered.update(found)
        elif (item.approved_text if side == APPROVED else item.suspect_text):
            searched.append(item)
    if not searched:
        return covered
    by_page: dict[int, list[int]] = {}
    for index, token in enumerate(tokens):
        if token.source_words:
            by_page.setdefault(token.source_words[0].page, []).append(index)
    for item in searched:
        location = item.approved_location if side == APPROVED else item.suspect_location
        text = item.approved_text if side == APPROVED else item.suspect_text
        if location is None or not text:
            continue
        # A repeated-header row stands for every copy: its boxes are the
        # copies' own, so being inside one is enough.
        available = None if FURNITURE_NOTE in text else Counter(text.split())
        for page, boxes in location.iter_pages():
            for index in by_page.get(page, []):
                if index in covered:
                    continue
                token = tokens[index]
                word = token.source_words[0]
                cx, cy = (word.x0 + word.x1) / 2.0, (word.top + word.bottom) / 2.0
                if not any(b[0] - 1 <= cx <= b[2] + 1 and b[1] - 1 <= cy <= b[3] + 1 for b in boxes):
                    continue
                if available is None:
                    covered.add(index)
                elif available[token.text] > 0:
                    available[token.text] -= 1
                    covered.add(index)
    return covered


def _page_list(pages: list[int]) -> str:
    """1, 2, 3, 5 -> "1-3, 5"."""
    parts: list[str] = []
    start = previous = None
    for page in pages:
        if previous is not None and page == previous + 1:
            previous = page
            continue
        if start is not None:
            parts.append(f"{start}" if start == previous else f"{start}–{previous}")
        start = previous = page
    if start is not None:
        parts.append(f"{start}" if start == previous else f"{start}–{previous}")
    return ", ".join(parts)
