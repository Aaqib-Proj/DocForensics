"""Diagnose why a pair of PDFs still diff with false positives.

Usage:
    python dev_tests/diagnose_pdf_pair.py <approved.pdf> <suspect.pdf>

For both documents it prints:
  * page count and every ruled table pdfplumber finds per page
    (a borderless/undetected table shows up as NO tables -- the most
    common reason the table-aware fix does not engage);
  * every token that was created by hyphen-stitching (2 words merged,
    e.g. the phantom "doubleWhen");
  * the diff items whose text mentions "double" / "When" (or any
    pattern passed after the two paths);
  * the token stream on both sides around every matching occurrence,
    so the reading-order context of the failure is visible.

Run it, then paste the output here (or give me the two file paths).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pdfplumber

from core.diff_engine import diff_documents
from core.pdf_extractor import (
    _TABLE_SETTINGS_LINES,
    build_comparison_tokens,
    extract_words,
)


def find_tables_on_page(page):
    return page.find_tables(table_settings=_TABLE_SETTINGS_LINES)


def analyze(path: str, label: str, patterns: list[str]) -> list[str]:
    print("=" * 70)
    print(f"{label}: {path}")
    print("=" * 70)

    lines_out: list[str] = []

    with pdfplumber.open(path) as pdf:
        print(f"pages: {len(pdf.pages)}")
        for p_idx, page in enumerate(pdf.pages, start=1):
            tables = find_tables_on_page(page)
            if not tables:
                print(f"  page {p_idx}: NO ruled tables detected")
            for t_idx, t in enumerate(tables):
                bbox = tuple(round(v, 1) for v in t.bbox)
                print(f"  page {p_idx} table {t_idx}: bbox={bbox}")
                print(f"    extract: {t.extract()}")

    words = extract_words(path)
    tokens = build_comparison_tokens(words)
    print(f"\n{label}: {len(words)} words -> {len(tokens)} tokens")

    stitched = [t for t in tokens if len(t.source_words) > 1]
    if stitched:
        print("hyphen-stitched tokens (2 words merged):")
        for t in stitched:
            print(
                f"    {t.text!r}  from {[w.text for w in t.source_words]!r} "
                f"cell_ids={[w.cell_id for w in t.source_words]}"
            )
    else:
        print("no hyphen-stitched tokens.")

    hits = [
        i for i, t in enumerate(tokens) if any(p in t.text.casefold() for p in patterns)
    ]
    if hits:
        print(f"occurrences of {patterns}: token indices {hits}")
        shown: set[int] = set()
        for h in hits:
            lo, hi = max(0, h - 8), min(len(tokens), h + 9)
            if any(i in shown for i in range(lo, hi)):
                continue
            shown.update(range(lo, hi))
            ctx = " ".join(
                f"**{t.text}**" if i == h else t.text for i, t in enumerate(tokens[lo:hi], start=lo)
            )
            print(f"    ... {ctx} ...")
    else:
        print(f"no tokens matching {patterns}.")

    lines_out.append(f"{label} tokens: {[t.text for t in tokens]}")
    return lines_out


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    approved, suspect = sys.argv[1], sys.argv[2]
    patterns = [p.casefold() for p in (sys.argv[3:] or ["double", "when"])]

    for path, label in ((approved, "APPROVED"), (suspect, "SUSPECT")):
        analyze(path, label, patterns)

    print("=" * 70)
    print("DIFF RESULTS (as the app would show them)")
    print("=" * 70)
    a_tokens = build_comparison_tokens(extract_words(approved))
    s_tokens = build_comparison_tokens(extract_words(suspect))
    items = diff_documents(a_tokens, s_tokens)
    noun = "discrepancy" if len(items) == 1 else "discrepancies"
    print(f"{len(items)} {noun} total.")
    for d in items:
        mentions = any(
            p in d.approved_text.casefold() or p in d.suspect_text.casefold()
            for p in patterns
        )
        marker = "  <-- matches your patterns" if mentions else ""
        print(f"  [{d.severity:8s}] {d.kind:20s} {d.approved_text!r} -> {d.suspect_text!r}{marker}")


if __name__ == "__main__":
    main()
