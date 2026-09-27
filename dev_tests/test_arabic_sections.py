import os
import sys

workspace_dir = r"c:\Users\aaqib\Downloads\docforensics-analyzer working"
if workspace_dir not in sys.path:
    sys.path.insert(0, workspace_dir)

import dev_tests.scratch_test as s
from dev_tests.heading_test import looks_like_heading
from core.diff_engine import _tokens_to_blocks, _Section, _match_sections, _diff_by_sections

blocks_app = _tokens_to_blocks(s.app_tokens)
blocks_susp_rtl = _tokens_to_blocks(s.susp_tokens_rtl)
blocks_susp_ltr = _tokens_to_blocks(s.susp_tokens_ltr)

print(f"App blocks: {len(blocks_app)}")
print(f"Susp RTL blocks: {len(blocks_susp_rtl)}")

def custom_build_section_map(blocks: list[dict]) -> dict[str, list[dict]]:
    sections: dict[str, list[dict]] = {}
    current_key = ""
    for block in blocks:
        text = block["text"].strip()
        if looks_like_heading(text) and not block.get("_in_table"):
            current_key = text
            sections.setdefault(current_key, []).append(block)
        else:
            sections.setdefault(current_key, []).append(block)
    return sections

sec_map_app = custom_build_section_map(blocks_app)
sec_map_susp_rtl = custom_build_section_map(blocks_susp_rtl)

print("\nApp sections found:", len(sec_map_app))
for k, v in sec_map_app.items():
    print(f"  Header: {k!r} -> {len(v)} blocks")

print("\nSusp RTL sections found:", len(sec_map_susp_rtl))
for k, v in sec_map_susp_rtl.items():
    print(f"  Header: {k!r} -> {len(v)} blocks")

# Let's test matching sections
secs_a = [_Section(tokens=[t for b in blist for t in b["_tokens"]], title=k) for k, blist in sec_map_app.items()]
secs_b_rtl = [_Section(tokens=[t for b in blist for t in b["_tokens"]], title=k) for k, blist in sec_map_susp_rtl.items()]

b_to_a, unmatched_a, unmatched_b = _match_sections(secs_a, secs_b_rtl)
print(f"\nMatched sections: {len(b_to_a)} matched, {len(unmatched_a)} unmatched A, {len(unmatched_b)} unmatched B")
for b_idx, a_idx in b_to_a.items():
    print(f"  Suspect [{secs_b_rtl[b_idx].title!r}] <---> Approved [{secs_a[a_idx].title!r}]")

diffs = _diff_by_sections(s.app_tokens, s.susp_tokens_rtl, secs_a, secs_b_rtl)
print(f"\nTotal diffs when diffing by matched sections: {len(diffs)}")
for d in diffs:
    print(f"  [{d.severity:8s}] {d.kind:20s} {d.approved_text!r} -> {d.suspect_text!r}")
