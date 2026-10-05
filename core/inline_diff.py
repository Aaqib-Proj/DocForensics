"""Word- and character-level diff INSIDE one flagged discrepancy.

`core.diff_engine` answers "which passages differ"; this module answers
"which part of THIS passage made it a discrepancy" -- the question the
comparator pane (ui/comparator_panel.py) puts in front of the reviewer:

    Original:  Store below 25 °C. Keep out of reach of children.
    Suspect:   Store below 30 °C. Keep out of reach of children.
                            ^^

Both texts are split into words AND the whitespace between them (so a
spacing-only change is visible, not invisible), diffed with difflib,
and returned as runs of "unchanged" / "changed" text per side. A
replaced run whose two sides have the same number of words is refined
one level deeper, character by character, so "25" vs "30" marks only
the digits that moved rather than the whole sentence.

Pure text in, pure text out -- no Qt, no colors, no HTML: the UI
decides how a changed run looks.
"""

from __future__ import annotations

import difflib
import re

# A run of text from one side: (changed, text). `changed` is True when
# this run is part of what makes the two sides differ. A changed run with
# EMPTY text is a caret: the point on this side where the other side has
# characters this one lacks ("Ropinirol|" against "Ropinirole"), so the
# reviewer sees where without anything unchanged being marked.
Segment = tuple[bool, str]

_TOKEN_RE = re.compile(r"\s+|\S+")

# Two replaced words are refined character by character only when they
# are recognizably the same word edited ("Ropinirole"/"Ropinirol"); a
# lower similarity means a genuinely different word, where per-character
# marks would be meaningless noise ("mg"/"tablets").
_MIN_CHAR_REFINE_RATIO = 0.4


def _tokens(text: str) -> list[str]:
    """Words and the whitespace runs between them, in order."""
    return _TOKEN_RE.findall(text)


def _merge(segments: list[Segment]) -> list[Segment]:
    """Join neighboring runs with the same flag into one."""
    merged: list[Segment] = []
    for changed, text in segments:
        if not text and not changed:
            continue
        if merged and merged[-1][0] == changed:
            merged[-1] = (changed, merged[-1][1] + text)
        else:
            merged.append((changed, text))
    return merged


def _refine_chars(word_a: str, word_b: str) -> tuple[list[Segment], list[Segment]]:
    """Diff one replaced word against another, character by character."""
    matcher = difflib.SequenceMatcher(None, word_a, word_b, autojunk=False)
    if matcher.ratio() < _MIN_CHAR_REFINE_RATIO:
        return [(True, word_a)], [(True, word_b)]
    left: list[Segment] = []
    right: list[Segment] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            left.append((False, word_a[i1:i2]))
            right.append((False, word_b[j1:j2]))
        else:
            # An empty side becomes a caret at this position.
            left.append((True, word_a[i1:i2]))
            right.append((True, word_b[j1:j2]))
    return left, right


def inline_segments(approved: str, suspect: str) -> tuple[list[Segment], list[Segment]]:
    """Split ``approved`` and ``suspect`` into unchanged/changed runs.

    Returns (approved segments, suspect segments). Concatenating the
    text of either side reproduces that side's input exactly, so the
    reviewer always sees the real text -- only the marking is added.

    A pure insertion or deletion (one side empty) comes back as one
    changed run on the non-empty side and an empty list on the other.
    """
    if not approved and not suspect:
        return [], []
    if not approved:
        return [], [(True, suspect)]
    if not suspect:
        return [(True, approved)], []

    tokens_a, tokens_b = _tokens(approved), _tokens(suspect)
    matcher = difflib.SequenceMatcher(None, tokens_a, tokens_b, autojunk=False)
    left: list[Segment] = []
    right: list[Segment] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        slice_a, slice_b = tokens_a[i1:i2], tokens_b[j1:j2]
        if tag == "equal":
            left.append((False, "".join(slice_a)))
            right.append((False, "".join(slice_b)))
        elif tag == "delete":
            left.append((True, "".join(slice_a)))
            right.append((True, ""))  # caret: deleted from here
        elif tag == "insert":
            left.append((True, ""))  # caret: inserted here
            right.append((True, "".join(slice_b)))
        elif not "".join(slice_a).strip() and not "".join(slice_b).strip():
            # Whitespace against different whitespace. Marked whole on
            # BOTH sides: a reviewer has to see where the spacing
            # differs in each document, and a single space refined
            # character by character would leave one side unmarked.
            left.append((True, "".join(slice_a)))
            right.append((True, "".join(slice_b)))
        elif len(slice_a) == len(slice_b):
            # Same number of words on both sides: the words pair up, so
            # each pair can be compared character by character.
            for word_a, word_b in zip(slice_a, slice_b):
                part_a, part_b = _refine_chars(word_a, word_b)
                left.extend(part_a)
                right.extend(part_b)
        else:
            left.append((True, "".join(slice_a)))
            right.append((True, "".join(slice_b)))
    return _merge(left), _merge(right)


def changed_words(approved: str, suspect: str) -> tuple[str, str]:
    """The differing WORDS of each side ("Ropinirole 5", "Ropinirol 50")
    -- the one-line "25 -> 30" summary of what made this a discrepancy.

    Whole words, not the character runs `inline_segments` marks: a
    reviewer reading a summary needs the word that changed, not the one
    letter inside it ("e -> 0" says nothing). Both sides come back
    empty when only whitespace differs.
    """
    words_a, words_b = approved.split(), suspect.split()
    matcher = difflib.SequenceMatcher(None, words_a, words_b, autojunk=False)
    left: list[str] = []
    right: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            left.append(" ".join(words_a[i1:i2]))
        if j2 > j1:
            right.append(" ".join(words_b[j1:j2]))
    return " / ".join(left), " / ".join(right)
