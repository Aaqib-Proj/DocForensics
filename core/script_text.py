"""Superscript/subscript text representation (core/script_text.py).

Superscripts and subscripts are kept visible in extracted text, so
"2⁴" and "24", or "H₂O" and "H2O", compare as different -- they are
different content on a label (an exponent, a footnote reference, a
chemical formula), not a formatting nit.

Every extractor (PDF, DOCX, PPTX, XLSX) funnels raised/lowered runs
through ``to_script`` so all formats produce the identical text for the
same content. Characters with a Unicode superscript/subscript form are
written with it ("2⁴", "H₂O", "1ˢᵗ"); a run containing any letter or
digit without one is written as ``^{...}`` / ``_{...}`` instead, so no
meaningful script text is flattened back to baseline text. A run of
unmappable punctuation only (a raised ".") is left as plain text.

No Qt imports -- standalone-testable.
"""

from __future__ import annotations

SUPERSCRIPT = "sup"
SUBSCRIPT = "sub"

_SUPERSCRIPT_MAP = {
    **dict(zip("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")),
    "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
    **dict(zip("abcdefghijklmnoprstuvwxyz", "ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ")),
    **dict(zip("ABDEGHIJKLMNOPRTUVW", "ᴬᴮᴰᴱᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂ")),
}

_SUBSCRIPT_MAP = {
    **dict(zip("0123456789", "₀₁₂₃₄₅₆₇₈₉")),
    "+": "₊", "-": "₋", "−": "₋", "=": "₌", "(": "₍", ")": "₎",
    **dict(zip("aehijklmnoprstuvx", "ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ")),
}

_MAPS = {SUPERSCRIPT: _SUPERSCRIPT_MAP, SUBSCRIPT: _SUBSCRIPT_MAP}
_MARKERS = {SUPERSCRIPT: "^", SUBSCRIPT: "_"}

# Every character ``to_script`` can emit. Text normalization must leave
# these alone (NFKC would fold "⁴" back to "4").
SCRIPT_CHARS = frozenset(
    ch for mapping in _MAPS.values() for ch in mapping.values()
)

# Superscript/subscript digits, for numeric-change detection.
SCRIPT_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉"


def to_script(text: str, kind: str | None) -> str:
    """Render one raised (``"sup"``) or lowered (``"sub"``) run of text.

    ``kind=None`` returns ``text`` unchanged. Whitespace inside the run
    is kept as-is.
    """
    if not kind or not text:
        return text
    mapping = _MAPS[kind]
    if all(ch in mapping or ch.isspace() for ch in text):
        return "".join(mapping.get(ch, ch) for ch in text)
    if not any(ch.isalnum() for ch in text):
        # A raised/lowered "." or "," carries no meaning of its own (PDF
        # producers shift punctuation glyphs slightly all the time).
        return text
    return f"{_MARKERS[kind]}{{{text}}}"


def join_runs(runs) -> str:
    """Join ``(text, kind)`` pairs into one string, rendering each
    raised/lowered run with ``to_script``. Adjacent runs of the same
    kind are merged first, so a partly-unmappable run gets one
    ``^{...}`` wrapper rather than one per formatting run."""
    merged: list[list] = []
    for text, kind in runs:
        if not text:
            continue
        if merged and merged[-1][1] == kind:
            merged[-1][0] += text
        else:
            merged.append([text, kind])
    return "".join(to_script(text, kind) for text, kind in merged)
