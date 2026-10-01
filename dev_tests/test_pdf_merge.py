"""Tests for PDF merging and split document comparison (dev_tests/test_pdf_merge.py)."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
import pytest
from PySide6.QtWidgets import QApplication

from core.diff_engine import diff_documents
from core.document_extractor import extract_document_tokens
from core.pdf_merge import (
    format_file_size,
    inspect_document,
    merge_pdf_documents,
    natural_sort_key,
)
from ui.document_panel import DocumentPanel
from ui.merge_dialog import MergeDocumentsDialog

# Ensure single QApplication instance for tests
@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def test_natural_sort_key():
    files = ["doc_part10.pdf", "doc_part1.pdf", "doc_part2.pdf", "doc_part20.pdf", "doc_part3.pdf"]
    sorted_files = sorted(files, key=natural_sort_key)
    assert sorted_files == [
        "doc_part1.pdf",
        "doc_part2.pdf",
        "doc_part3.pdf",
        "doc_part10.pdf",
        "doc_part20.pdf",
    ]


def test_format_file_size():
    assert format_file_size(500) == "500 B"
    assert format_file_size(2048) == "2.0 KB"
    assert format_file_size(1024 * 1024 * 3) == "3.0 MB"


def test_inspect_document(tmp_path):
    # Non-existent file
    info_none = inspect_document(str(tmp_path / "missing.pdf"))
    assert not info_none["is_valid"]
    assert "File not found" in info_none["error"]

    # Valid PDF with 2 pages
    p = str(tmp_path / "valid.pdf")
    doc = fitz.open()
    doc.new_page(width=200, height=200).insert_text((20, 20), "Page 1")
    doc.new_page(width=200, height=200).insert_text((20, 20), "Page 2")
    doc.save(p)
    doc.close()

    info_valid = inspect_document(p)
    assert info_valid["is_valid"]
    assert info_valid["page_count"] == 2
    assert info_valid["name"] == "valid.pdf"
    assert info_valid["error"] is None


def test_merge_and_diff_paragraphs(tmp_path):
    """Verify the core client requirement:
    Original has all content in 1 document.
    Suspect is split into 2 PDFs (half in part 1, half in part 2).
    Comparing individually fails/shows missing text;
    merging both and comparing matches all paragraphs with 0 differences!
    """
    orig_path = str(tmp_path / "original_full.pdf")
    susp_part1 = str(tmp_path / "suspect_part1.pdf")
    susp_part2 = str(tmp_path / "suspect_part2.pdf")

    # 1. Create full original document (2 pages, paragraphs A, B on page 1; C on page 2)
    doc_orig = fitz.open()
    p1 = doc_orig.new_page(width=500, height=500)
    p1.insert_text((50, 50), "1 INDICATIONS AND USAGE\nThis medication is approved for clinical care.")
    p1.insert_text((50, 150), "2 DOSAGE AND ADMINISTRATION\nTake one tablet daily with water.")
    p2 = doc_orig.new_page(width=500, height=500)
    p2.insert_text((50, 50), "3 WARNINGS AND PRECAUTIONS\nKeep out of reach of children.")
    doc_orig.save(orig_path)
    doc_orig.close()

    # 2. Suspect part 1: only has page 1 content
    doc_s1 = fitz.open()
    p_s1 = doc_s1.new_page(width=500, height=500)
    p_s1.insert_text((50, 50), "1 INDICATIONS AND USAGE\nThis medication is approved for clinical care.")
    p_s1.insert_text((50, 150), "2 DOSAGE AND ADMINISTRATION\nTake one tablet daily with water.")
    doc_s1.save(susp_part1)
    doc_s1.close()

    # 3. Suspect part 2: only has page 2 content
    doc_s2 = fitz.open()
    p_s2 = doc_s2.new_page(width=500, height=500)
    p_s2.insert_text((50, 50), "3 WARNINGS AND PRECAUTIONS\nKeep out of reach of children.")
    doc_s2.save(susp_part2)
    doc_s2.close()

    # Compare part 1 alone vs original -> shows missing section 3
    t_orig = extract_document_tokens(orig_path)
    t_s1 = extract_document_tokens(susp_part1)
    diffs_s1 = diff_documents(t_orig, t_s1)
    assert len(diffs_s1) > 0, "Comparing only part 1 should report differences (missing part 2)"

    # Compare part 2 alone vs original -> shows missing sections 1 & 2
    t_s2 = extract_document_tokens(susp_part2)
    diffs_s2 = diff_documents(t_orig, t_s2)
    assert len(diffs_s2) > 0, "Comparing only part 2 should report differences (missing part 1)"

    # 4. Now merge suspect part 1 + suspect part 2
    merged_path, file_infos = merge_pdf_documents([susp_part1, susp_part2])
    assert os.path.exists(merged_path)
    assert len(file_infos) == 2
    assert file_infos[0]["start_page"] == 1
    assert file_infos[0]["end_page"] == 1
    assert file_infos[1]["start_page"] == 2
    assert file_infos[1]["end_page"] == 2

    # Verify merged PDF page count
    doc_merged = fitz.open(merged_path)
    assert doc_merged.page_count == 2
    doc_merged.close()

    # 5. Extract tokens from merged document and compare with original
    t_merged = extract_document_tokens(merged_path)
    matches = []
    diffs_merged = diff_documents(t_orig, t_merged, matches)

    # ALL PARAGRAPHS MATCH: 0 differences!
    assert len(diffs_merged) == 0, f"Expected 0 differences after merge, got: {[d.kind for d in diffs_merged]}"


def test_merge_dialog_and_panel_ui(qapp, tmp_path):
    """Test MergeDocumentsDialog interaction, reordering, and DocumentPanel display."""
    f1 = str(tmp_path / "part_a.pdf")
    f2 = str(tmp_path / "part_b.pdf")

    d1 = fitz.open()
    d1.new_page(width=300, height=300).insert_text((30, 30), "Part A text")
    d1.save(f1)
    d1.close()

    d2 = fitz.open()
    d2.new_page(width=300, height=300).insert_text((30, 30), "Part B text")
    d2.save(f2)
    d2.close()

    # 1. Dialog creation and table check
    dialog = MergeDocumentsDialog(initial_files=[f1, f2], title="Suspect", can_compare=True)
    assert dialog.table.rowCount() == 2
    assert dialog.btn_merge.isEnabled()
    assert dialog.btn_merge.text() == "Merge & Compare"

    # Reorder
    dialog.table.selectRow(0)
    dialog.on_move_down_clicked()
    assert dialog._files == [f2, f1]

    # Reorder back
    dialog.table.selectRow(1)
    dialog.on_move_up_clicked()
    assert dialog._files == [f1, f2]

    # Execute merge
    dialog.on_merge_clicked()
    assert dialog.merged_path is not None
    assert os.path.exists(dialog.merged_path)
    assert dialog.should_compare is True

    # 2. Panel display
    panel = DocumentPanel("Suspect")
    assert panel.merge_button is not None
    panel.load_merged_document(dialog.merged_path, dialog.source_files, dialog.file_infos)

    assert panel.file_path == dialog.merged_path
    assert panel.source_files == [f1, f2]
    assert "Merged (2 files)" in panel.file_label.text()
    assert "Merged Document (2 files, 2 total pages)" in panel.file_label.toolTip()
