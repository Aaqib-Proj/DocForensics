"""Headless end-to-end check of the Compare button + results + navigate flow.

Not part of dev_tests/ (it drives real Qt widgets, unlike the other
smoke tests which stay Qt-free) -- this is a one-off manual
verification script for this milestone, run with QT_QPA_PLATFORM=offscreen.
"""
import dataclasses
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog, QApplication, QGestureEvent, QMessageBox, QPinchGesture

from main import MainWindow

APPROVED_TEXT = (
    "This document describes the recommended dosage of pharmacokinetics "
    "medication for adult patients. The patient should take one tablet "
    "twice daily with food. Do not exceed the maximum recommended dose."
)
SUSPECT_TEXT = (
    "This document describes the recommended dosage of pharmacokinetics "
    "medication for adult patients. The patient should take ONE tablet "
    "daily with food! Do not exceed the maximum recommended daily dose."
)
# vs approved: removed 'twice', added 'daily', 'one'->'ONE' (major),
# and 'food.' -> 'food!' (punctuation-only -> minor).


def make_pdf(path, text, width):
    doc = fitz.open()
    page = doc.new_page(width=width, height=300)
    rect = fitz.Rect(36, 36, width - 36, 264)
    page.insert_textbox(rect, text, fontsize=11, fontname="helv")
    doc.save(path)
    doc.close()


def make_two_page(path, page1_height, page2_text):
    """Two-page PDF: a spacer page 1 (so a page-2 diff bbox lands at a
    different scene Y when page 1 heights differ) and a text page 2."""
    doc = fitz.open()
    page1 = doc.new_page(width=400, height=page1_height)
    page1.insert_textbox(
        fitz.Rect(36, 36, 364, page1_height - 36), "header text",
        fontsize=11, fontname="helv",
    )
    page2 = doc.new_page(width=400, height=300)
    page2.insert_textbox(
        fitz.Rect(36, 200, 364, 290), page2_text, fontsize=11, fontname="helv"
    )
    doc.save(path)
    doc.close()


def make_sectioned_pdf(path, sections, edit=None):
    """Numbered all-caps section headers with bodies, stacked top to
    bottom (MVP-8 out-of-order section matching scenario)."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=900)
    y = 40.0
    for num, title, body in sections:
        page.insert_text(fitz.Point(36, y), f"{num} {title}", fontsize=11, fontname="helv")
        y += 16
        text = body
        if edit and edit[0] == title:
            text = body.replace(*edit[1])
        page.insert_textbox(fitz.Rect(36, y, 576, y + 130), text, fontsize=10, fontname="helv")
        y += 145
    doc.save(path)
    doc.close()


SECTIONED_APPROVED = [
    ("1", "INDICATIONS AND USAGE", "Zolpidem tartrate tablets are indicated for the short term treatment of insomnia characterized by difficulties with sleep initiation."),
    ("2", "DOSAGE AND ADMINISTRATION", "The recommended dose for adults is ten milligrams once daily immediately before bedtime."),
    ("3", "WARNINGS AND PRECAUTIONS", "Elderly or debilitated patients may be especially sensitive to the effects of zolpidem tartrate. Patients with hepatic impairment should use a lower dose."),
    ("4", "ADVERSE REACTIONS", "The most common adverse reactions include drowsiness dizziness and diarrhea."),
]


def make_three_page(path, page1_height, page2_height, page3_text):
    """Three-page PDF: text on page 3 only, so a diff on that text has
    no corresponding page in a 2-page counterpart."""
    doc = fitz.open()
    doc.new_page(width=400, height=page1_height)
    doc.new_page(width=400, height=page2_height)
    page3 = doc.new_page(width=400, height=300)
    page3.insert_textbox(
        fitz.Rect(36, 200, 364, 290), page3_text, fontsize=11, fontname="helv"
    )
    doc.save(path)
    doc.close()


def main():
    tmp = tempfile.mkdtemp()
    # Reviews are kept per pair of files, by content. This suite builds
    # byte-identical PDFs every run, so it must not share the real store
    # (or its own previous run's): each run starts from no decisions.
    os.environ["DOCFORENSICS_REVIEW_DIR"] = os.path.join(tmp, "reviews")
    approved_path = os.path.join(tmp, "approved.pdf")
    suspect_path = os.path.join(tmp, "suspect.pdf")
    make_pdf(approved_path, APPROVED_TEXT, 550)
    make_pdf(suspect_path, SUSPECT_TEXT, 260)

    dialogs = []
    QMessageBox.critical = staticmethod(lambda *args: dialogs.append(args[2]))

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()  # lay the panels out so scrollbars have real geometry
    app.processEvents()

    print("1. Compare button initially disabled:", not win.compare_button.isEnabled())

    win.original_panel.render_pdf(approved_path)
    win.original_panel.file_path = approved_path
    print("2. After loading Original only, Compare still disabled:", not win.compare_button.isEnabled())

    win.suspect_panel.render_pdf(suspect_path)
    win.suspect_panel.file_path = suspect_path
    print("3. After loading both, Compare enabled:", win.compare_button.isEnabled())

    results = {}

    def on_finished(items):
        results["items"] = items

    win.results_panel.item_activated.connect(lambda *_: None)  # no-op, just checking wiring
    win.on_compare_clicked()

    # DiffWorker runs on QThreadPool -- pump the event loop until finished.
    deadline = time.time() + 10
    while win.results_panel.rowCount() == 0 and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)

    n_rows = win.results_panel.rowCount()
    print(f"4. Results table populated: {n_rows} row(s) -> {'PASS' if n_rows > 0 else 'FAIL'}")
    print("   Status label:", win.status_label.text())

    # --- Ignore-minor toggle (MVP-7): hides minor and below, keeps the rest ---
    n_all = win.results_panel.rowCount()
    win.results_panel.ignore_minor_checkbox.setChecked(True)
    n_filtered = win.results_panel.rowCount()
    win.results_panel.ignore_minor_checkbox.setChecked(False)
    n_restored = win.results_panel.rowCount()
    ok_toggle = (
        n_filtered < n_all
        and n_filtered > 0
        and n_restored == n_all
        and len(win.results_panel._items) == n_all  # underlying results untouched
    )
    print(f"4b. 'Ignore minor and below' hides minor rows ({n_all} -> {n_filtered}, "
          f"restores to {n_restored}): {'PASS' if ok_toggle else 'FAIL'}")

    # Simulate clicking the first result row.
    first_item = win.results_panel._items[0]
    win.original_panel.clear_highlight()
    win.suspect_panel.clear_highlight()
    win.on_result_activated(first_item)
    highlighted = (
        bool(win.original_panel._highlight_items)
        or bool(win.suspect_panel._highlight_items)
    )
    print(f"5. Clicking a result draws a highlight: {'PASS' if highlighted else 'FAIL'}")
    print(f"   Diff clicked: [{first_item.severity}] {first_item.kind} "
          f"{first_item.approved_text!r} -> {first_item.suspect_text!r}")

    # --- Missing-text navigation (MVP-7): mirror the before coordinates
    #     onto the panel that lacks the text ---
    removed_item = next(d for d in win.results_panel._items if d.kind == "removed_in_suspect")
    added_item = next(d for d in win.results_panel._items if d.kind == "added_in_suspect")
    changed_item = next(d for d in win.results_panel._items if d.kind == "text_changed")

    win.original_panel.clear_highlight()
    win.suspect_panel.clear_highlight()
    win.on_result_activated(removed_item)
    ok_missing = (
        bool(win.original_panel._highlight_items)
        and bool(win.suspect_panel._missing_items)
    )
    print("5a. Removed text draws a 'missing' marker on the other panel:",
          "PASS" if ok_missing else "FAIL")

    win.original_panel.clear_highlight()
    win.suspect_panel.clear_highlight()
    win.on_result_activated(added_item)
    ok_missing2 = (
        bool(win.suspect_panel._highlight_items)
        and bool(win.original_panel._missing_items)
    )
    print("5b. Added text mirrors back onto the Original panel:",
          "PASS" if ok_missing2 else "FAIL")

    win.original_panel.clear_highlight()
    win.suspect_panel.clear_highlight()
    win.on_result_activated(changed_item)
    ok_both = (
        bool(win.original_panel._highlight_items)
        and bool(win.suspect_panel._highlight_items)
        and not win.original_panel._missing_items
        and not win.suspect_panel._missing_items
    )
    print("5c. Changed text (present on both) gets no missing marker:",
          "PASS" if ok_both else "FAIL")

    # --- 5d: the mirrored scrollbar must NOT yank a panel away from its
    #     own marker. Use a page-2 removed word with DIFFERENT page-1
    #     heights, so the same bbox maps to a different scene Y on each
    #     side -- the mirror would scroll the Original off its highlight.
    tall_approved = os.path.join(tmp, "tall_approved.pdf")
    tall_suspect = os.path.join(tmp, "tall_suspect.pdf")
    make_two_page(tall_approved, 300, "alpha beta gamma delta epsilon zeta")
    make_two_page(tall_suspect, 500, "alpha beta gamma epsilon zeta")  # 'delta' removed
    win.original_panel.render_pdf(tall_approved)
    win.suspect_panel.render_pdf(tall_suspect)
    win.original_panel.file_path = tall_approved
    win.suspect_panel.file_path = tall_suspect
    win.on_compare_clicked()
    deadline = time.time() + 10
    while win.results_panel.rowCount() == 0 and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    removed_tall = next(d for d in win.results_panel._items if d.kind == "removed_in_suspect")
    loc = removed_tall.approved_location

    # Where the Original panel SHOULD land: navigate it alone first.
    win.original_panel.clear_highlight()
    win.suspect_panel.clear_highlight()
    win.original_panel.highlight_pdf_bbox(loc.page, loc.boxes or [loc.bbox])
    expected_v = win.original_panel.view.verticalScrollBar().value()
    expected_h = win.original_panel.view.horizontalScrollBar().value()

    # Now the real click path, starting from a deliberately different spot.
    win.original_panel.clear_highlight()
    win.suspect_panel.clear_highlight()
    win.original_panel.view.verticalScrollBar().setValue(0)
    win.original_panel.view.horizontalScrollBar().setValue(0)
    win.on_result_activated(removed_tall)
    actual_v = win.original_panel.view.verticalScrollBar().value()
    actual_h = win.original_panel.view.horizontalScrollBar().value()
    ok_nav = (
        abs(actual_v - expected_v) <= 2
        and abs(actual_h - expected_h) <= 2
        and bool(win.original_panel._highlight_items)
        and bool(win.suspect_panel._missing_items)
    )
    print(f"5d. Missing-text navigation stays on the right coordinates "
          f"(page {loc.page}, expected v/h {expected_v}/{expected_h}, "
          f"actual {actual_v}/{actual_h}):", "PASS" if ok_nav else "FAIL")

    # --- 5e: missing text on a page the other document doesn't have at
    #     all (e.g. original page 3 vs a 2-page suspect). The panel must
    #     still navigate -- clamping the marker to the bottom of its
    #     last page -- and the status bar must explain the mismatch.
    three_approved = os.path.join(tmp, "three_approved.pdf")
    two_suspect = os.path.join(tmp, "two_suspect.pdf")
    make_three_page(three_approved, 300, 300, "alpha beta gamma delta epsilon zeta")
    make_two_page(two_suspect, 300, "alpha beta gamma epsilon zeta")  # 'delta' is on p3, suspect has no p3
    win.original_panel.render_pdf(three_approved)
    win.suspect_panel.render_pdf(two_suspect)
    win.original_panel.file_path = three_approved
    win.suspect_panel.file_path = two_suspect
    win.on_compare_clicked()
    deadline = time.time() + 10
    while win.results_panel.rowCount() == 0 and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    removed_p3 = next(
        d for d in win.results_panel._items
        if d.kind == "removed_in_suspect" and d.approved_location is not None
        and d.approved_location.page == 3
    )
    # With an insertion-point caret (the normal case since MVP-15) the
    # Suspect marks where the text would be in its own layout.
    win.on_result_activated(removed_p3)
    app.processEvents()
    ok_caret = bool(win.suspect_panel._missing_items) and removed_p3.suspect_caret is not None
    # Without one, the marker clamps to the Suspect's last page and the
    # status bar explains the page-count mismatch.
    no_caret = dataclasses.replace(removed_p3, suspect_caret=None)
    win.original_panel.clear_highlight()
    win.suspect_panel.clear_highlight()
    win.original_panel.view.verticalScrollBar().setValue(0)
    win.suspect_panel.view.verticalScrollBar().setValue(0)
    win.on_result_activated(no_caret)
    app.processEvents()
    ok_missing_page = (
        ok_caret
        and bool(win.original_panel._highlight_items)
        and bool(win.suspect_panel._missing_items)
        and win.suspect_panel.view.verticalScrollBar().value() > 0  # it actually navigated
        and "Suspect has only 2 page(s)" in win.status_label.text()
    )
    print("5e. Missing text on a page the other doc lacks still navigates "
          "(clamped marker on its last page, status explains it):",
          "PASS" if ok_missing_page else "FAIL")
    print("    status:", win.status_label.text())

    # --- 5f: a multi-line discrepancy draws ONE highlight rect per visual
    #     line (Location.boxes), not a single tall merged rectangle that
    #     slices through unrelated content. Narrow page so the paragraph
    #     wraps; suspect loses the trailing words, spanning 2+ lines.
    narrow_approved = os.path.join(tmp, "narrow_approved.pdf")
    narrow_suspect = os.path.join(tmp, "narrow_suspect.pdf")
    long_para = (
        "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu "
        "nu xi omicron pi rho sigma tau upsilon phi chi psi omega"
    )
    make_pdf(narrow_approved, long_para, 180)
    make_pdf(narrow_suspect, " ".join(long_para.split()[:14]), 180)  # removed tail
    win.original_panel.render_pdf(narrow_approved)
    win.suspect_panel.render_pdf(narrow_suspect)
    win.original_panel.file_path = narrow_approved
    win.suspect_panel.file_path = narrow_suspect
    win.on_compare_clicked()
    deadline = time.time() + 10
    while win.results_panel.rowCount() == 0 and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    removed_mult = next(
        d for d in win.results_panel._items if d.kind == "removed_in_suspect"
        and d.approved_location is not None
    )
    loc = removed_mult.approved_location
    n_lines = len(loc.boxes)
    win.original_panel.clear_highlight()
    win.suspect_panel.clear_highlight()
    win.on_result_activated(removed_mult)
    app.processEvents()
    drawn = win.original_panel._highlight_items
    ys = sorted({round(r.rect().y(), 1) for r in drawn})
    ok_multiline = (
        n_lines >= 2  # the diff really spans several visual lines
        and len(drawn) == n_lines  # one drawn rect per line
        and len(ys) == n_lines  # distinct vertical positions, not one tall box
    )
    print(f"5f. Multi-line diff draws {n_lines} per-line highlights "
          f"({len(drawn)} rects at {len(ys)} distinct Ys):",
          "PASS" if ok_multiline else "FAIL")

    # --- Trackpad zoom (MVP-7): Ctrl+wheel and synthesized pinch ---
    def ctrl_wheel(view, delta_y):
        # Real wheel events land on the scroll area's VIEWPORT, which Qt
        # routes up to QGraphicsView.wheelEvent(); synthetic tests must
        # target the viewport for the same path.
        event = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            QPoint(0, 0),
            QPoint(0, delta_y),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.ControlModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(view.viewport(), event)

    def pinch(view, scale):
        gesture = QPinchGesture()
        gesture.setScaleFactor(scale)
        QApplication.sendEvent(view, QGestureEvent([gesture]))

    for panel in (win.original_panel, win.suspect_panel):
        panel.set_zoom(1.0)
    before = win.original_panel.zoom_factor()
    suspect_before = win.suspect_panel.zoom_factor()
    ctrl_wheel(win.original_panel.view, 120)  # one notch in
    after = win.original_panel.zoom_factor()
    suspect_after = win.suspect_panel.zoom_factor()
    print("6. Ctrl+wheel zooms only that viewer:",
          "PASS" if after > before and suspect_after == suspect_before else "FAIL")
    print(f"   original {before:.3f} -> {after:.3f}, suspect {suspect_before:.3f} -> {suspect_after:.3f}, "
          f"label={win.original_panel.zoom_label.text()}")

    before = win.original_panel.zoom_factor()
    ctrl_wheel(win.original_panel.view, -120)  # one notch out
    after = win.original_panel.zoom_factor()
    print("7. Ctrl+wheel out zooms back down:", "PASS" if after < before else "FAIL")

    # Zooming keeps the content under the cursor in place (with room to
    # scroll both ways -- at a page edge the view can't move further).
    view = win.original_panel.view
    win.original_panel.set_zoom(3.0)
    view.centerOn(win.original_panel.scene.sceneRect().center())
    cursor = view.viewport().rect().center() + QPoint(40, 30)
    under_cursor = view.mapToScene(cursor)
    event = QWheelEvent(
        QPointF(cursor), QPointF(cursor), QPoint(0, 0), QPoint(0, 240),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    QApplication.sendEvent(view.viewport(), event)
    drift = view.mapFromScene(under_cursor) - cursor
    print("7b. Zoom keeps the point under the cursor in place:",
          "PASS" if drift.manhattanLength() <= 2 else "FAIL", f"(drift {drift.x()},{drift.y()} px)")
    win.original_panel.set_zoom(before)

    before_orig = win.original_panel.zoom_factor()
    before_susp = win.suspect_panel.zoom_factor()
    pinch(win.suspect_panel.view, 1.2)  # pinch-out gesture on the suspect panel
    after_orig = win.original_panel.zoom_factor()
    after_susp = win.suspect_panel.zoom_factor()
    print("8. Pinch gesture zooms only the pinched viewer:",
          "PASS" if after_susp > before_susp and after_orig == before_orig else "FAIL")
    print(f"   original {before_orig:.3f} -> {after_orig:.3f} / suspect {before_susp:.3f} -> {after_susp:.3f}")

    before = win.original_panel.zoom_factor()
    plain = QWheelEvent(
        QPointF(10, 10), QPointF(10, 10),
        QPoint(0, 0), QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(win.original_panel.view.viewport(), plain)
    unchanged = abs(win.original_panel.zoom_factor() - before) < 1e-9
    print("9. Plain wheel scroll does NOT zoom:", "PASS" if unchanged else "FAIL")

    # --- Out-of-order sections (MVP-8): a section moved to the front
    #     shows as one 'Reordered Section' row that navigates both
    #     panels, with the genuine edit inside it still critical. ---
    sec_approved = os.path.join(tmp, "sec_approved.pdf")
    sec_suspect = os.path.join(tmp, "sec_suspect.pdf")
    make_sectioned_pdf(sec_approved, SECTIONED_APPROVED)
    moved = [SECTIONED_APPROVED[2], SECTIONED_APPROVED[0],
             SECTIONED_APPROVED[1], SECTIONED_APPROVED[3]]
    make_sectioned_pdf(sec_suspect, moved,
                       edit=("WARNINGS AND PRECAUTIONS", ("especially", "very")))
    win.original_panel.render_pdf(sec_approved)
    win.suspect_panel.render_pdf(sec_suspect)
    win.original_panel.file_path = sec_approved
    win.suspect_panel.file_path = sec_suspect
    win.on_compare_clicked()
    deadline = time.time() + 10
    while win.results_panel.rowCount() == 0 and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    move_rows = [d for d in win.results_panel._items if d.kind == "reordered_section"]
    critical_rows = [d for d in win.results_panel._items if d.severity == "critical"]
    win.original_panel.clear_highlight()
    win.suspect_panel.clear_highlight()
    if move_rows:
        win.on_result_activated(move_rows[0])
        app.processEvents()
        ok_move_nav = (
            bool(win.original_panel._highlight_items)
            and bool(win.suspect_panel._highlight_items)
        )
    else:
        ok_move_nav = False
    ok_sections = (
        len(move_rows) == 1
        and move_rows[0].approved_text == "3 WARNINGS AND PRECAUTIONS"
        and move_rows[0].severity == "reorder"
        and len(critical_rows) == 1
        and critical_rows[0].approved_text == "especially"
        and ok_move_nav
    )
    print("10. Moved section -> one 'Reordered Section' row; click navigates "
          "both panels; edit inside stays critical:",
          "PASS" if ok_sections else "FAIL")
    print(f"    rows: {[(d.kind, d.severity) for d in win.results_panel._items]}")

    # --- Viewer coordination: independent zoom, lock, two-way navigation ---
    win.original_panel.render_pdf(approved_path)
    win.suspect_panel.render_pdf(suspect_path)
    win.on_compare_clicked()
    deadline = time.time() + 10
    while win._active_worker is not None and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    items = win.results_panel._items
    orig, susp = win.original_panel, win.suspect_panel

    orig.set_zoom(1.0)
    susp.set_zoom(1.0)
    orig.zoom_in_button.click()
    print("11. A viewer's own + button zooms only that viewer:",
          "PASS" if orig.zoom_factor() > 1.0 and susp.zoom_factor() == 1.0 else "FAIL")

    # Clicking a result gives the other viewer the zoom of the viewer used last.
    orig.set_zoom(1.0)
    susp.set_zoom(1.0)
    orig.zoom_by(2.0)  # the user zooms the Original to 200%
    changed = next(d for d in items if d.kind == "text_changed")
    win.on_result_activated(changed)
    print("12. Clicking a difference gives the other viewer the same zoom:",
          "PASS" if abs(susp.zoom_factor() - 2.0) < 1e-6 and susp._highlight_items else "FAIL",
          f"(suspect {susp.zoom_factor():.2f})")

    # Lock: zooming/scrolling one moves the other.
    win.lock_checkbox.setChecked(True)
    orig.zoom_by(1.25)
    zoom_locked = abs(susp.zoom_factor() - orig.zoom_factor()) < 1e-6
    susp_before = susp.view.verticalScrollBar().value()
    orig.view.verticalScrollBar().setValue(orig.view.verticalScrollBar().maximum())
    app.processEvents()
    scroll_locked = susp.view.verticalScrollBar().value() != susp_before
    win.lock_checkbox.setChecked(False)
    orig.zoom_by(1.25)
    unlocked = abs(susp.zoom_factor() - orig.zoom_factor()) > 1e-6
    print("13. Lock views syncs zoom and scrolling; unlocking makes them independent:",
          "PASS" if zoom_locked and scroll_locked and unlocked else "FAIL",
          f"(zoom {zoom_locked}, scroll {scroll_locked}, unlocked {unlocked})")

    def click_pdf_point(panel, page, x, y):
        """A real mouse click on the viewer at a PDF point."""
        panel.center_on_position(page, x, y)
        app.processEvents()
        rect = panel._scene_rects_for(page, [(x, y, x, y)])[0]
        pos = panel.view.mapFromScene(rect.center())
        QTest.mouseClick(panel.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
        app.processEvents()

    # Viewer -> table: clicking a highlighted difference selects its row.
    win.results_panel.table.clearSelection()
    orig.clear_highlight()
    susp.clear_highlight()
    target = next(d for d in items if d.kind == "text_changed" and d.approved_location)
    x0, top, x1, bottom = target.approved_location.bbox
    click_pdf_point(orig, target.approved_location.page, (x0 + x1) / 2, (top + bottom) / 2)
    row = win.results_panel.table.currentRow()
    selected = win.results_panel._visible_items[row] if row >= 0 else None
    print("14. Clicking a difference in a viewer selects its row and navigates both:",
          "PASS" if selected is target and orig._highlight_items and susp._highlight_items else "FAIL")

    # Viewer -> other viewer: clicking plain text shows the same text there.
    token_map = win._token_map
    word_index = next(i for i, t in enumerate(token_map._tokens["approved"]) if t.text == "document")
    word = token_map._tokens["approved"][word_index].source_words[0]
    orig.clear_highlight()
    susp.clear_highlight()
    click_pdf_point(orig, word.page, (word.x0 + word.x1) / 2, (word.top + word.bottom) / 2)
    match_index, exact = token_map.counterpart("approved", word_index)
    match = token_map._tokens["suspect"][match_index].source_words[0]
    marker = susp._highlight_items[0].rect() if susp._highlight_items else None
    expected = susp._scene_rects_for(match.page, [match.bbox])[0]
    visible = susp.view.mapToScene(susp.view.viewport().rect()).boundingRect()
    ok_sync = (
        exact
        and marker is not None
        and abs(marker.center().x() - expected.center().x()) < 1
        and abs(marker.center().y() - expected.center().y()) < 1
        and visible.contains(marker.center())
        and "same text" in win.status_label.text()
    )
    print("15. Clicking text in one viewer takes the other to the same text:",
          "PASS" if ok_sync else "FAIL", f"| {win.status_label.text()}")

    # Table -> viewers with the keyboard.
    win.results_panel.table.setFocus()
    win.results_panel.select_item(win.results_panel._visible_items[0])
    orig.clear_highlight()
    susp.clear_highlight()
    QTest.keyClick(win.results_panel.table, Qt.Key.Key_Down)
    app.processEvents()
    moved = win.results_panel.table.currentRow() == 1
    print("16. Arrow keys in the results table navigate the viewers:",
          "PASS" if moved and (orig._highlight_items or orig._missing_items) else "FAIL")


    # --- Interactive deviation list: categories, filters, review, mass actions ---
    from ui.results_panel import ACCEPTED, REJECTED

    rp = win.results_panel
    win.original_panel.render_pdf(approved_path)
    win.suspect_panel.render_pdf(suspect_path)
    win.on_compare_clicked()
    deadline = time.time() + 10
    while win._active_worker is not None and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    categories = sorted(rp.category(d) for d in rp.all_items())
    print("17. Differences are categorized:",
          "PASS" if categories == ["added", "case", "punctuation", "removed"] else "FAIL", categories)

    total = rp.rowCount()
    overlays_before = len(win.original_panel._overlay_items) + len(win.suspect_panel._overlay_items)
    rp._chips["removed"].setChecked(False)
    hidden_rows = rp.rowCount()
    overlays_after = len(win.original_panel._overlay_items) + len(win.suspect_panel._overlay_items)
    rp._chips["removed"].setChecked(True)
    rp.search_box.setText("daily")
    searched = [d.suspect_text for d in rp._visible_items]
    rp.search_box.clear()
    print("18. Category chips and text search filter the list (and the page highlights):",
          "PASS" if hidden_rows == total - 1 and overlays_after < overlays_before
          and searched == ["daily"] and rp.rowCount() == total else "FAIL",
          f"(rows {total}->{hidden_rows}, overlays {overlays_before}->{overlays_after}, search {searched})")

    win.reviewer_edit.setText("QA Reviewer")
    rp.table.clearSelection()
    rp.table.selectRow(0)
    first = rp._visible_items[0]
    rp.decide_selected(ACCEPTED)
    decision = rp.decision(first)
    status_text = rp.table.item(0, 0).text()
    overlays_accepted = len(win.original_panel._overlay_items) + len(win.suspect_panel._overlay_items)
    rp.hide_reviewed_checkbox.setChecked(True)
    rows_hiding_reviewed = rp.rowCount()
    rp.hide_reviewed_checkbox.setChecked(False)
    rp.table.selectRow(1)
    QTest.keyClick(rp.table, Qt.Key.Key_R)
    rejected = rp.decision(rp._visible_items[1])
    print("19. Accept/Reject record reviewer + time; accepted drops off the pages; 'Hide reviewed' works:",
          "PASS" if decision and decision.status == ACCEPTED and decision.reviewer == "QA Reviewer"
          and "Accepted" in status_text and overlays_accepted < overlays_before
          and rows_hiding_reviewed == total - 1 and rejected and rejected.status == REJECTED else "FAIL",
          f"({status_text!r}, rows hiding reviewed {rows_hiding_reviewed})")

    repeated_a = os.path.join(tmp, "repeated_a.pdf")
    repeated_b = os.path.join(tmp, "repeated_b.pdf")
    sentence = "The colour of the tablet is white. Store the tablet in a dry place."
    make_pdf(repeated_a, " ".join([sentence] * 5), 550)
    make_pdf(repeated_b, " ".join([sentence.replace("colour", "color")] * 5), 550)
    win.original_panel.render_pdf(repeated_a)
    win.suspect_panel.render_pdf(repeated_b)
    win.on_compare_clicked()
    deadline = time.time() + 10
    while win._active_worker is not None and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    ungrouped_rows = rp.rowCount()
    rp.group_checkbox.setChecked(True)
    grouped_rows = rp.rowCount()
    count_text = rp.table.item(0, 3).text()
    rp.table.selectRow(0)
    rp.decide_selected(REJECTED)
    all_rejected = all(rp.decision(d) and rp.decision(d).status == REJECTED for d in rp.all_items())
    rp.group_checkbox.setChecked(False)
    rp.decide(rp.identical_to(rp._visible_items[0]), ACCEPTED)  # the right-click "all identical" action
    all_accepted = all(rp.decision(d).status == ACCEPTED for d in rp.all_items())
    print("20. 'Group identical' folds 5 identical changes into one row; one decision covers all:",
          "PASS" if ungrouped_rows == 5 and grouped_rows == 1 and count_text == "\u00d75"
          and all_rejected and all_accepted else "FAIL",
          f"(rows {ungrouped_rows}->{grouped_rows}, count {count_text!r})")

    # --- 21. The comparator pane: what changed inside the selected row ---
    comparator = rp.comparator
    comparator.context_checkbox.setChecked(False)
    rp.select_item(rp._visible_items[0])
    app.processEvents()
    # "‸" is the caret marking where the other side has a letter
    # this side lacks ("colo‸r" against "colo[u]r").
    original_text = comparator.original_view.toPlainText().replace("‸", "")
    suspect_text = comparator.suspect_view.toPlainText().replace("‸", "")
    spelled_out = "colour" in original_text and "color" in suspect_text
    # With "Context" off the pane shows the difference by itself; with
    # it on, the surrounding words come from the comparison's own token
    # map. (The box defaults to off, so the bare difference is what a
    # reviewer sees first.)
    bare = "tablet" not in original_text
    comparator.context_checkbox.setChecked(True)
    app.processEvents()
    in_context = (
        "tablet" in comparator.original_view.toPlainText()
        and "tablet" in comparator.suspect_view.toPlainText()
    )
    comparator.context_checkbox.setChecked(False)
    app.processEvents()
    # Only the letter that differs is marked (a tinted run in the HTML).
    marks = comparator.original_view.toHtml().count("background-color")
    summary = comparator.summary_label.text()
    decision_shown = "Accepted by QA Reviewer" in comparator.status_label.text()
    pages_shown = "page 1" in comparator.pages_label.text()

    # A difference clicked in a VIEWER spells itself out here too.
    comparator.clear()
    changed = next(d for d in rp.all_items() if d.kind == "text_changed")
    win._activate_from_viewer(changed, win.original_panel)
    app.processEvents()
    from_viewer = "colour" in comparator.original_view.toPlainText()

    # And it can be hidden and brought back (View > Comparator).
    win.comparator_action.setChecked(False)
    hidden = not rp.comparator_visible()
    win.comparator_action.setChecked(True)
    back = rp.comparator_visible() and "colour" in comparator.original_view.toPlainText()

    print("21. Comparator spells out the selected difference (bare, in context, marked, both ways):",
          "PASS" if spelled_out and bare and in_context and marks == 1
          and summary == "colour   →   color"
          and decision_shown and pages_shown and from_viewer and hidden and back else "FAIL",
          f"(bare {bare}, context {in_context}, marks {marks}, summary {summary!r})")

    # --- 22. Double-click a difference in a viewer to rematch it ---
    # A warning paragraph the suspect really does drop: double-clicking
    # it searches the whole Suspect and confirms the deletion instead of
    # re-pointing it at something that merely shares a few words.
    warning = ("Keep this medicine out of the sight and reach of children at all times "
               "during the whole of the treatment period.")
    dose = "The recommended starting dose is one tablet taken three times every day."
    store = "Store below 25 C in a dry place away from direct sunlight and moisture."
    gone_a = os.path.join(tmp, "rematch_a.pdf")
    gone_b = os.path.join(tmp, "rematch_b.pdf")
    make_pdf(gone_a, warning + "\n\n" + dose + "\n\n" + store, 420)
    make_pdf(gone_b, dose + "\n\n" + store, 420)
    win.original_panel.render_pdf(gone_a)
    win.suspect_panel.render_pdf(gone_b)
    win.original_panel.file_path = gone_a
    win.suspect_panel.file_path = gone_b
    rp.group_checkbox.setChecked(False)
    rp._chips["moved"].setChecked(True)  # show every row for this check
    win.on_compare_clicked()
    deadline = time.time() + 20
    while win._active_worker is not None and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)

    rows_before = len(rp.all_items())
    target = next(
        (d for d in rp.all_items()
         if len((d.approved_text or d.suspect_text).split()) >= 4
         and (d.approved_location or d.suspect_location)),
        None,
    )
    confirmed = False
    message = ""
    if target is not None:
        panel = win.original_panel if target.approved_location else win.suspect_panel
        loc = target.approved_location or target.suspect_location
        # The center of ONE highlighted line: the union box of a
        # multi-line paragraph has its center in the gap between lines,
        # where nothing is highlighted.
        x0, top, x1, bottom = (loc.boxes or [loc.bbox])[0]
        win._on_viewer_double_clicked(panel, loc.page, (x0 + x1) / 2, (top + bottom) / 2)
        app.processEvents()
        message = win.status_label.text()
        # Genuinely deleted: the row must survive the search untouched.
        confirmed = ("genuine" in message
                     and any(d is target for d in rp.all_items())
                     and len(rp.all_items()) == rows_before)

    # Replacing a row and undoing it (what a rematch that DOES find a
    # counterpart goes through).
    from core.diff_engine import DiffItem
    replacement = DiffItem(
        index=0, kind="reordered", severity="reorder",
        approved_text=target.approved_text, suspect_text=target.approved_text,
        approved_location=target.approved_location, suspect_location=None,
    )
    rp.replace_item(target, [replacement])
    app.processEvents()
    replaced = (any(d is replacement for d in rp.all_items())
                and not any(d is target for d in rp.all_items()))
    win._undo_rematch()
    app.processEvents()
    undone = (any(d is target for d in rp.all_items())
              and not any(d is replacement for d in rp.all_items())
              and len(rp.all_items()) == rows_before
              and not rp.can_undo_rematch())

    # The gesture itself must reach the window, not just the handler.
    seen = []
    win.original_panel.point_double_clicked.connect(lambda *a: seen.append(a))
    view = win.original_panel.view
    center = view.viewport().rect().center()
    QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton,
                      Qt.KeyboardModifier.NoModifier, center)
    app.processEvents()
    clicked_ok = bool(seen)

    print("22. Double-clicking a difference in a viewer rematches just that one:",
          "PASS" if confirmed and replaced and undone and clicked_ok else "FAIL",
          f"({message[:60]!r}, replaced {replaced}, undo {undone}, gesture {clicked_ok})")

    # --- 23. The comparator's letter-by-letter view ---
    from core.diff_engine import DiffItem as _DiffItem, Location as _Location

    def _loc(page):
        box = (10.0, 10.0, 90.0, 20.0)
        return _Location(page=page, bbox=box, boxes=[box], boxes_by_page={page: [box]})

    comparator.show_item(
        _DiffItem(0, "text_changed", "critical", "dd/mm/yy", "06/01/25", _loc(1), _loc(1))
    )
    app.processEvents()
    letters = comparator.letters_view.toPlainText()
    date_ok = (comparator.letters_view.isVisible()
               and "U+0064" in letters and "LATIN SMALL LETTER D" in letters
               and "U+0030" in letters and "DIGIT ZERO" in letters
               and "letter changed to a digit" in letters)

    # A Cyrillic look-alike: identical on the page, different in the file.
    comparator.show_item(
        _DiffItem(0, "text_changed", "critical", "paracetamol", "parаcetamol",
                  _loc(1), _loc(1))
    )
    app.processEvents()
    lookalike = comparator.letters_view.toPlainText()
    lookalike_ok = "CYRILLIC SMALL LETTER A" in lookalike and "look-alike" in lookalike

    # One-sided differences have nothing to line up, and the view says so.
    comparator.show_item(
        _DiffItem(0, "removed_in_suspect", "critical", "Keep out of reach", "", _loc(1), None)
    )
    app.processEvents()
    one_sided_ok = "nothing to line up" in comparator.letters_view.toPlainText()

    # And it can be switched off.
    comparator.letters_checkbox.setChecked(False)
    app.processEvents()
    hidden_ok = not comparator.letters_view.isVisible()
    comparator.letters_checkbox.setChecked(True)
    app.processEvents()

    print("23. Comparator compares the selected difference letter by letter:",
          "PASS" if date_ok and lookalike_ok and one_sided_ok and hidden_ok else "FAIL",
          f"(date {date_ok}, look-alike {lookalike_ok}, one-sided {one_sided_ok}, toggle {hidden_ok})")
    # --- 24. The review survives: streaming, a re-compare, the report ---
    def compare_and_wait():
        win.on_compare_clicked()
        deadline = time.time() + 20
        while win._active_worker is not None and time.time() < deadline:
            app.processEvents()
            time.sleep(0.02)
        app.processEvents()

    rev_a = os.path.join(tmp, "review_a.pdf")
    rev_b = os.path.join(tmp, "review_b.pdf")
    make_pdf(rev_a, "Take 5 mg daily with food. Store below 25 C. Keep out of reach of children.", 400)
    make_pdf(rev_b, "Take 10 mg daily with food. Store below 30 C. Keep out of sight of children.", 400)
    win.original_panel.render_pdf(rev_a)
    win.suspect_panel.render_pdf(rev_b)
    win.original_panel.file_path = rev_a
    win.suspect_panel.file_path = rev_b
    win.reviewer_edit.setText("QA Reviewer")

    # A decision taken while rows are still streaming in survives the
    # authoritative list arriving at the end.
    win.on_compare_clicked()
    early = None
    deadline = time.time() + 20
    while win._active_worker is not None and time.time() < deadline:
        app.processEvents()
        if early is None and rp.all_items():
            early = rp.all_items()[0]
            rp.decide([early], REJECTED)
        time.sleep(0.01)
    app.processEvents()
    kept_streaming = early is None or (early in rp.all_items() and rp.decision(early) is not None)

    first = rp.all_items()[0]
    rp.decide([first], ACCEPTED)
    win._flush_review()
    decided_text = (first.approved_text, first.suspect_text)

    # Same two files compared again: the decision comes back.
    compare_and_wait()
    again = next((d for d in rp.all_items() if (d.approved_text, d.suspect_text) == decided_text), None)
    restored = again is not None and rp.decision(again) is not None and rp.decision(again).status == ACCEPTED

    # Page order walks the Original top to bottom.
    rp.sort_combo.setCurrentIndex(1)
    app.processEvents()
    tops = [d.approved_location.bbox[1] for d in rp._visible_items if d.approved_location is not None]
    page_ordered = tops == sorted(tops)
    rp.sort_combo.setCurrentIndex(0)

    # The report: both SHA-256s, the decision, the reviewer.
    from core.review_store import file_digest
    report_path = os.path.join(tmp, "report.html")
    original_dialog = QFileDialog.getSaveFileName
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (report_path, "HTML report (*.html)"))
    try:
        win._export_report()
    finally:
        QFileDialog.getSaveFileName = original_dialog
    report = open(report_path, encoding="utf-8").read() if os.path.exists(report_path) else ""
    reported = (file_digest(rev_a) in report and file_digest(rev_b) in report
                and "QA Reviewer" in report and "accepted" in report)

    # A near-empty suspect: the red banner says so, and the dialog asks
    # for acknowledgement.
    empty_b = os.path.join(tmp, "stamp_only.pdf")
    doc24 = fitz.open()
    doc24.new_page(width=400, height=300).insert_text((36, 40), "By Packaging Development at 4:26 pm", fontsize=9, fontname="helv")
    doc24.save(empty_b)
    doc24.close()
    long_a = os.path.join(tmp, "long_a.pdf")
    doc24 = fitz.open()
    doc24.new_page(width=600, height=800).insert_textbox(
        fitz.Rect(36, 36, 564, 764), " ".join(f"word{i}" for i in range(260)), fontsize=9, fontname="helv"
    )
    doc24.save(long_a)
    doc24.close()
    win.original_panel.render_pdf(long_a)
    win.suspect_panel.render_pdf(empty_b)
    win.original_panel.file_path = long_a
    win.suspect_panel.file_path = empty_b
    compare_and_wait()
    banner = rp.coverage_banner.isVisible() and "yielded only" in rp.coverage_banner.text()
    warned = [w for w in app.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible()]
    for box in warned:
        box.close()

    print("24. The review survives streaming and a re-compare, sorts by page, exports, and warns:",
          "PASS" if kept_streaming and restored and page_ordered and reported and banner and warned else "FAIL",
          f"(streaming {kept_streaming}, restored {restored}, page order {page_ordered}, "
          f"report {reported}, banner {banner}, dialog {bool(warned)})")
    print("dialogs:", dialogs or "none")


if __name__ == "__main__":
    main()
