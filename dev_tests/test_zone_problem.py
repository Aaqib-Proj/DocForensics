import sys
import os

workspace_dir = r"c:\Users\aaqib\Downloads\docforensics-analyzer working"
if workspace_dir not in sys.path:
    sys.path.insert(0, workspace_dir)

import dev_tests.scratch_test as s
from core.pdf_extractor import (
    Word, _detect_column_bands, _group_into_lines, _order_body_units,
    _text_is_rtl, _fix_rtl_word_chars, _normalize_word_text, build_comparison_tokens
)
from core.diff_engine import diff_documents

def make_column_words(text: str, page: int, col_x0: float, col_width: float, base_y: float) -> list[Word]:
    words = []
    lines = text.strip().splitlines()
    y = base_y
    for line in lines:
        if not line.strip():
            y += 14.0
            continue
        line_words = line.strip().split()
        x = col_x0
        # wrap words within col_width
        for w in line_words:
            is_rtl = _text_is_rtl(w)
            raw = _fix_rtl_word_chars(w) if is_rtl else w
            norm = _normalize_word_text(raw, mirror_brackets=is_rtl)
            word_w = max(len(w) * 5.0, 15.0)
            if x + word_w > col_x0 + col_width and x > col_x0:
                y += 14.0
                x = col_x0
            words.append(Word(text=norm, page=page, x0=x, top=y, x1=x + word_w, bottom=y + 11.0))
            x += word_w + 5.0
        y += 14.0
    return words

app_words = (
    make_column_words(s.p1_text, page=1, col_x0=50, col_width=400, base_y=50) +
    make_column_words(s.p2_text, page=2, col_x0=50, col_width=400, base_y=50) +
    make_column_words(s.p3_text, page=3, col_x0=50, col_width=400, base_y=50) +
    make_column_words(s.p4_text, page=4, col_x0=50, col_width=400, base_y=50)
)

# Suspect with 4 columns:
# Col 1: x=50..170 (width 120)
# Gutter: 170..195 (width 25)
# Col 2: x=195..315 (width 120)
# Gutter: 315..340 (width 25)
# Col 3: x=340..460 (width 120)
# Gutter: 460..485 (width 25)
# Col 4: x=485..605 (width 120)

susp_words = (
    make_column_words(s.col1_text, page=1, col_x0=50, col_width=120, base_y=50) +
    make_column_words(s.col2_text, page=1, col_x0=195, col_width=120, base_y=50) +
    make_column_words(s.col3_text, page=1, col_x0=340, col_width=120, base_y=50) +
    make_column_words(s.col4_text, page=1, col_x0=485, col_width=120, base_y=50)
)

susp_dicts = [w.__dict__ for w in susp_words]
rows = sorted(_group_into_lines(susp_dicts), key=lambda r: sum(w["top"] for w in r)/len(r))
print(f"Total suspect rows: {len(rows)}")

x0 = min(w["x0"] for w in susp_dicts)
x1 = max(w["x1"] for w in susp_dicts)

page_bands = _detect_column_bands(rows, x0, x1)
print(f"Page-level detected bands ({len(page_bands)}): {page_bands}")

app_tokens = build_comparison_tokens(app_words)
susp_tokens = build_comparison_tokens(susp_words)

diffs = diff_documents(app_tokens, susp_tokens)
print(f"\nDiff results with current codebase: {len(diffs)} items")
for d in diffs:
    print(f"  [{d.severity:8s}] {d.kind:20s} {d.approved_text[:40]!r} -> {d.suspect_text[:40]!r}")

