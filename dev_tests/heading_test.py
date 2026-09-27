import re
import unicodedata

# Common sentence-ending punctuation in Arabic and other languages
_SENTENCE_END_PUNCT = {".", "!", "?", "؟", "،", ",", ";", "؛"}
_BULLET_CHARS = {"", "•", "-", "*", "–", "—", "\uf0b7"}

def is_arabic_or_non_latin_text(text: str) -> bool:
    for ch in text:
        bidi = unicodedata.bidirectional(ch)
        if bidi in {"R", "AL"}:
            return True
    return False

def looks_like_heading(line_text: str) -> bool:
    tokens = line_text.strip().split()
    if not tokens:
        return False
    # Check if starts with bullet
    if tokens[0] in _BULLET_CHARS or (len(tokens[0]) > 0 and tokens[0][0] in _BULLET_CHARS):
        return False
    # Check length
    if len(tokens) > 6 or len(line_text) > 70:
        return False
    # Check letters
    alpha_count = sum(ch.isalpha() for ch in line_text)
    if alpha_count < 3:
        return False
    # Check sentence ending
    last_char = line_text.strip()[-1]
    if last_char in _SENTENCE_END_PUNCT:
        return False
    return True

