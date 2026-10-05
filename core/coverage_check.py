"""What the comparison could NOT see (core/coverage_check.py).

A comparison can only vouch for text it extracted. When one side yields
almost nothing -- a scan carrying one digital stamp, pages that are
images, a font with no text mapping -- the diff is still computed, and
"2 differences" then reads as "nearly identical" when it really means
"nearly nothing was compared". A real case: a 4-page leaflet whose
suspect extracted 9 words ("By Packaging Development at 4:26 pm, Sep 05,
2023") against 6,653 in the approved.

`coverage_warnings` names those gaps in the reviewer's terms, so the
results can be read for what they are. No Qt: standalone-testable.
"""

from __future__ import annotations

import os
from typing import Optional

from core.pdf_extractor import MIN_DIGITAL_TEXT_CHARS, ComparisonToken

# One side yielding less than this share of the other's words, once the
# larger side is a real document, is not an edit -- it is text the
# extraction could not see.
_MIN_WORD_RATIO = 0.25
_MIN_WORDS_TO_JUDGE = 200
# Words containing U+FFFD: glyphs the PDF's font gives no character for.
_MAX_UNDECODED_SHARE = 0.03
_UNDECODED = "�"


def coverage_is_serious(
    approved_tokens: list[ComparisonToken], suspect_tokens: list[ComparisonToken]
) -> bool:
    """Whether one side yielded so few words that the result must not be
    read at all without the reviewer acknowledging it -- the case that
    gets a dialog, not just the banner. Blank pages and undecodable
    fonts are common in real artwork and stay in the banner: a dialog on
    every comparison would only teach reviewers to click it away."""
    counts = (len(approved_tokens), len(suspect_tokens))
    return max(counts) >= _MIN_WORDS_TO_JUDGE and min(counts) < max(counts) * _MIN_WORD_RATIO


def coverage_warnings(
    approved_path: str,
    suspect_path: str,
    approved_tokens: list[ComparisonToken],
    suspect_tokens: list[ComparisonToken],
) -> list[str]:
    """Plain-language warnings about content the comparison could not
    read, for both documents. Empty when both were fully readable."""
    warnings: list[str] = []
    sides = (
        ("Original", approved_path, approved_tokens),
        ("Suspect", suspect_path, suspect_tokens),
    )

    counts = {name: len(tokens) for name, _path, tokens in sides}
    larger = max(counts.values())
    if larger >= _MIN_WORDS_TO_JUDGE:
        for name, count in counts.items():
            other = "Suspect" if name == "Original" else "Original"
            if count < larger * _MIN_WORD_RATIO:
                warnings.append(
                    f"The {name} yielded only {count:,} word{'s' if count != 1 else ''} against "
                    f"{counts[other]:,} in the {other}. It is probably a scan, or its text is "
                    "outlined or embedded as images -- the comparison cannot vouch for content "
                    "it could not read, so a short list of differences here does NOT mean the "
                    "documents match."
                )

    for name, path, tokens in sides:
        empty_pages = _pages_without_text(path)
        if empty_pages:
            warnings.append(
                f"{name}: {_describe_pages(empty_pages)} no extractable text (blank, or printed "
                "as an image/scan). Anything on "
                f"{'it' if len(empty_pages) == 1 else 'them'} was not compared."
            )
        undecoded = sum(1 for t in tokens if _UNDECODED in t.text)
        if tokens and undecoded / len(tokens) > _MAX_UNDECODED_SHARE:
            warnings.append(
                f"{name}: {undecoded:,} of {len(tokens):,} words ({undecoded / len(tokens):.0%}) "
                "contain characters the PDF's font gives no text for (shown as �). "
                "Those passages cannot be compared reliably -- check them visually."
            )
    return warnings


def _layout_pdf(path: str) -> Optional[str]:
    """The PDF whose pages the comparison read, if there is one."""
    from core.document_extractor import document_kind
    from core.office_convert import cached_pdf_path

    try:
        kind = document_kind(path)
    except Exception:  # noqa: BLE001 -- an unknown file has no pages to check
        return None
    if kind == "pdf":
        return path
    if kind == "docx":
        converted = cached_pdf_path(path)
        return converted if os.path.exists(converted) else None
    return None


def _pages_without_text(path: str) -> list[int]:
    pdf = _layout_pdf(path)
    if pdf is None:
        return []
    import fitz

    try:
        with fitz.open(pdf) as doc:
            return [
                number
                for number, page in enumerate(doc, start=1)
                if sum(1 for ch in page.get_text() if not ch.isspace()) < MIN_DIGITAL_TEXT_CHARS
            ]
    except Exception:  # noqa: BLE001 -- an unreadable file fails extraction with its own error
        return []


def _describe_pages(pages: list[int]) -> str:
    """[2, 3, 4, 7] -> "pages 2-4 and 7 have"."""
    runs: list[str] = []
    start = previous = pages[0]
    for page in pages[1:] + [None]:
        if page is not None and page == previous + 1:
            previous = page
            continue
        runs.append(str(start) if start == previous else f"{start}–{previous}")
        if page is not None:
            start = previous = page
    label = " and ".join([", ".join(runs[:-1]), runs[-1]] if len(runs) > 1 else runs)
    return f"page {label} has" if len(pages) == 1 else f"pages {label} have"
