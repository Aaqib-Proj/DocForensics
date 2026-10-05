"""Letter-by-letter comparison of one difference (core/char_compare.py).

`core.inline_diff` says WHICH words and characters differ, for marking
them in running text. This module answers the next question a proofer
asks about a flagged difference:

    exactly which character is it, and what kind of change is this?

    'E'  U+0045  LATIN CAPITAL LETTER E
    'e'  U+0065  LATIN SMALL LETTER E
    -> upper to lower case

Every differing character is identified by its code point and its
Unicode name, and the change is classified, because the classification
is usually the finding: a digit that changed is a dosage error, a
Cyrillic "а" where a Latin "a" belongs is invisible on the page but
wrong in the file, a non-breaking space that became an ordinary one
will reflow, and a case transition is a house-style issue.

The two texts are also aligned character by character
(`align_characters`) so the UI can print one above the other with the
differences in the same columns, the way a proofing station does.

No Qt, no colors: standalone-testable.
"""

from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass
from typing import Optional

# One aligned column: the character on each side ("" where that side
# has nothing), and whether they differ.
AlignedColumn = tuple[str, str, bool]

# Characters that carry no ink. Shown by name, never as a blank, so a
# space-for-space change is visible.
_INVISIBLE = {
    " ": "space",
    " ": "no-break space",
    " ": "figure space",
    " ": "narrow no-break space",
    " ": "thin space",
    "​": "zero width space",
    "‌": "zero width non-joiner",
    "‍": "zero width joiner",
    "﻿": "zero width no-break space",
    "\t": "tab",
    "\n": "line feed",
}

# Script prefixes of Unicode names, for spotting a letter that looks
# right but comes from another alphabet.
_SCRIPTS = ("LATIN", "CYRILLIC", "GREEK", "ARABIC", "HEBREW", "CJK", "HIRAGANA", "KATAKANA")


@dataclass
class CharChange:
    """One differing character (or one inserted/deleted character)."""

    approved: str  # "" when the character was added in the suspect
    suspect: str  # "" when the character was removed in the suspect
    index: int  # position in the approved text (insertion point when added)
    note: str  # what kind of change this is, in words

    @property
    def kind(self) -> str:
        if not self.approved:
            return "added"
        if not self.suspect:
            return "removed"
        return "changed"


def describe(char: str) -> str:
    """``'E' U+0045 LATIN CAPITAL LETTER E`` for one character."""
    if not char:
        return "(nothing)"
    shown = _INVISIBLE.get(char)
    label = f"({shown})" if shown else repr(char)
    try:
        name = unicodedata.name(char)
    except ValueError:
        name = unicodedata.category(char)
    return f"{label} U+{ord(char):04X} {name}"


def align_characters(approved: str, suspect: str) -> list[AlignedColumn]:
    """The two texts as aligned columns, one character per column.

    A character present on only one side gets an empty cell opposite
    it, so the two lines stay in step and the differences sit in the
    same columns when printed one above the other.
    """
    columns: list[AlignedColumn] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, approved, suspect, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            columns.extend((char, char, False) for char in approved[i1:i2])
            continue
        left, right = approved[i1:i2], suspect[j1:j2]
        for offset in range(max(len(left), len(right))):
            columns.append(
                (
                    left[offset] if offset < len(left) else "",
                    right[offset] if offset < len(right) else "",
                    True,
                )
            )
    return columns


def character_changes(approved: str, suspect: str) -> list[CharChange]:
    """Every differing character, in order, each with its own note."""
    changes: list[CharChange] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, approved, suspect, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        left, right = approved[i1:i2], suspect[j1:j2]
        for offset in range(max(len(left), len(right))):
            a = left[offset] if offset < len(left) else ""
            b = right[offset] if offset < len(right) else ""
            changes.append(CharChange(a, b, i1 + offset, classify(a, b)))
    return changes


def classify(approved: str, suspect: str) -> str:
    """What kind of change one character pair is, in a reviewer's words."""
    if approved and not suspect:
        return f"{_what(approved)} removed"
    if suspect and not approved:
        return f"{_what(suspect)} added"
    if approved == suspect:
        return "no change"

    if approved.isdecimal() and suspect.isdecimal():
        return "digit changed"
    if approved.isdecimal() != suspect.isdecimal():
        return "digit changed to a letter" if approved.isdecimal() else "letter changed to a digit"

    if approved.lower() == suspect.lower():
        return (
            "upper to lower case"
            if approved.isupper() and suspect.islower()
            else "lower to upper case"
            if approved.islower() and suspect.isupper()
            else "case changed"
        )

    base_a, base_b = _strip_accents(approved), _strip_accents(suspect)
    if base_a == base_b and base_a:
        return "accent changed"

    script_a, script_b = _script(approved), _script(suspect)
    if script_a and script_b and script_a != script_b:
        # "LATIN SMALL LETTER A" and "CYRILLIC SMALL LETTER A" are the
        # same letter to the eye and different characters in the file:
        # the one error a proofreader cannot see on the page.
        if _name_without_script(approved) == _name_without_script(suspect):
            return f"look-alike letter: {script_b.lower()} instead of {script_a.lower()}"
        return f"{script_a.lower()} letter changed to {script_b.lower()}"

    if approved in _INVISIBLE or suspect in _INVISIBLE:
        return f"{_what(approved)} changed to {_what(suspect)}"
    if not approved.isalnum() and not suspect.isalnum():
        return "punctuation changed"
    if approved.isalnum() != suspect.isalnum():
        return "punctuation changed to a letter" if not approved.isalnum() else "letter changed to punctuation"
    return "letter changed"


def summary_note(changes: list[CharChange]) -> str:
    """One line naming what kinds of change this difference contains,
    the most serious first."""
    if not changes:
        return ""
    order = [
        "digit",
        "look-alike",
        "accent",
        "case",
        "space",
        "punctuation",
    ]
    notes = []
    for note in (change.note for change in changes):
        if note not in notes:
            notes.append(note)

    def rank(note: str) -> int:
        for index, word in enumerate(order):
            if word in note:
                return index
        return len(order)

    notes.sort(key=rank)
    return "; ".join(notes[:3])


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _what(char: str) -> str:
    """A short word for a character, for use inside a sentence."""
    if not char:
        return "nothing"
    if char in _INVISIBLE:
        return _INVISIBLE[char]
    if char.isdecimal():
        return "digit"
    if char.isalpha():
        return "letter"
    return "character"


def _strip_accents(char: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", char) if not unicodedata.combining(c))


def _script(char: str) -> Optional[str]:
    try:
        name = unicodedata.name(char)
    except ValueError:
        return None
    for script in _SCRIPTS:
        if name.startswith(script):
            return script
    return None


def _name_without_script(char: str) -> str:
    """"SMALL LETTER A" from "CYRILLIC SMALL LETTER A" -- what the
    character IS, with the alphabet it comes from taken off."""
    try:
        name = unicodedata.name(char)
    except ValueError:
        return ""
    script = _script(char)
    return name[len(script) :].strip() if script else name
