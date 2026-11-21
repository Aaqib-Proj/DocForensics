"""A single reusable panel: load button + QGraphicsView preview with zoom.

MVP-2 scope: replace the MVP-1 QLabel preview with QGraphicsView so
zoom and (in main.py) synchronized scrolling are possible.

MVP-4 addition: `highlight_pdf_bbox()` so a diff result (in PDF-point
coordinates from core/pdf_extractor.py) can be highlighted and scrolled
into view. At that point this panel was still page-1-only, so a bbox
on any other page was reported as unavailable.

MVP-6: multi-page navigation. Every page of the loaded PDF is now
rendered into ONE continuous, vertically-stacked QGraphicsScene (like
a normal PDF viewer's scroll view), each page positioned at its own Y
offset with a small gap between pages. `highlight_pdf_bbox()` can now
land on ANY page -- it just offsets the bbox by that page's Y position
in the scene -- so it only reports "unavailable" when the page
genuinely doesn't exist in that document (e.g. the Approved PDF has a
page the Suspect PDF doesn't). A small Prev/Next + "Page X / N"
control is also added so a reviewer can page through a document
directly, independent of any diff click.

MVP-7 additions:
  * Trackpad zoom. The view is a `_ZoomableGraphicsView` that maps a
    two-finger pinch (QPinchGesture) AND the Ctrl+wheel shortcut most
    touchpads emit for a pinch onto a relative zoom multiplier, emitted
    as `zoom_requested`. main.py applies the same factor to both
    panels, so pinching on either panel zooms them in lockstep -- no
    toolbar clicks required. Plain wheel scrolling is unaffected.
  * Missing-text markers. `highlight_missing_bbox()` draws a dashed red
    marker at the "before coordinates" of an insertion/deletion: for a
    word that only exists on one side, the OTHER panel shows where the
    text is absent, so a removed/added item navigates BOTH documents
    instead of just the one that still has the text.

MVP-13:
  * `highlight_location()`/`highlight_missing_location()` -- draw a
    discrepancy's highlight on EVERY page it touches
    (core.diff_engine.Location.iter_pages), not just its first page.
    Fixes a real bug where a discrepancy anchored to, say, page 3 of a
    multi-page document would fail to highlight AT ALL when checked
    against a shorter "concise" counterpart document, because the
    single-page `highlight_pdf_bbox()`/`highlight_missing_bbox()` this
    panel used to rely on exclusively could only ever look at one
    page. Those single-page methods still exist and still work for
    simple single-page callers.
  * `render_synthesized()` -- a plain text-flow preview for DOCX/PPTX/
    XLSX (formats with no PDF-style fixed page rendering), built from
    the exact same Word coordinates core.document_extractor produces
    for diffing, so the preview and the highlighter can never
    disagree about where a word is. `render_pdf()` (real rasterized
    pixels) is unchanged and still used for actual PDFs.

DOCX files are now laid out by Microsoft Word (or LibreOffice) into a
PDF in the background (core/office_convert.py) and shown with
`render_pdf()` -- real pages, table borders and positions, the same
PDF the comparison extracts from. `render_synthesized()` remains for
PPTX/XLSX, and for DOCX only when no layout engine is installed (the
panel then says the layout is approximate).

Independent viewers: each panel has its own zoom (toolbar, pinch,
Ctrl+wheel), anchored at the point being zoomed so the content under
the cursor stays put. Panels never move each other on their own --
main.py decides when to keep them in step ("Lock views") and when to
navigate the other panel (a click on a difference or on text), using
the signals below and the PDF-point position helpers.
"""

import logging
import os
import queue
import threading

import fitz  # PyMuPDF
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QGraphicsItem,
    QGestureEvent,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui import icons, theme
from workers.convert_worker import ConvertWorker

logger = logging.getLogger(__name__)

# PDF points -> render pixels. Must match the matrix used in
# render_pdf() below, since diff coordinates come back in raw PDF
# points and need to land on the same pixmap.
RENDER_DPI_SCALE = 200 / 72

# Vertical gap, in scene pixels, drawn between consecutive pages in the
# continuous-scroll stack -- purely cosmetic, so pages don't visually
# run into one another.
PAGE_GAP_PX = 18

_HIGHLIGHT_PEN = QPen(QColor(220, 30, 30), 2)
_HIGHLIGHT_BRUSH = QBrush(QColor(255, 235, 59, 90))  # translucent yellow

# "Same text" marker for click-to-sync between the two viewers.
_SYNC_PEN = QPen(QColor(25, 118, 210), 2)
_SYNC_BRUSH = QBrush(QColor(33, 150, 243, 60))

ZOOM_STEP = 1.25
MIN_ZOOM = 0.1
MAX_ZOOM = 8.0
# Mouse travel (px) below which a press+release counts as a click, not a drag.
_CLICK_SLOP = 5
# Empty canvas around the pages, in scene pixels.
_CANVAS_MARGIN = 24.0
# PDF pages are drawn only when near the view; at most this many stay
# drawn (the rest are released to keep memory flat on long documents).
_MAX_DRAWN_PAGES = 14
# Page images being prepared at once; more are requested as they arrive.
_MAX_PENDING_PAGES = 2
# Pages are drawn at a fraction of RENDER_DPI_SCALE matching the zoom
# (zoomed out, full resolution is wasted work and memory); a page is
# redrawn sharper when zooming in past its resolution.
_RESOLUTION_STEPS = (0.25, 0.5, 1.0)
_OPENABLE_EXTENSIONS = (".pdf", ".docx", ".pptx", ".xlsx", ".xlsm")
_OPEN_FILTER = (
    "Documents (*.pdf *.docx *.pptx *.xlsx *.xlsm);;"
    "PDF Files (*.pdf);;Word Documents (*.docx);;"
    "PowerPoint Files (*.pptx);;Excel Files (*.xlsx *.xlsm)"
)

# Marker for "this text is MISSING here": a dashed red outline (with a
# very light red fill) drawn at the coordinates the text OCCUPIES on
# the other document, so a reviewer can see at a glance where content
# was removed from / added to one side but not the other.
_MISSING_PEN = QPen(QColor(198, 40, 40), 2)
_MISSING_PEN.setStyle(Qt.PenStyle.DashLine)
_MISSING_BRUSH = QBrush(QColor(255, 235, 235, 50))

# Severity-colored overlays -- translucent fills so the underlying text
# remains readable while discrepancies are visually flagged at a glance.
# Alpha of ~55 gives a visible tint without obscuring text.
_SEVERITY_OVERLAY_COLORS = {
    "critical": QColor(198, 40, 40, 55),     # red
    "major":    QColor(230, 81, 0, 55),      # deep orange
    "minor":    QColor(249, 168, 37, 55),    # amber
    "typo":     QColor(97, 97, 97, 55),      # gray
}
_DEFAULT_OVERLAY_COLOR = QColor(120, 120, 120, 55)

# Pen colors matching each severity -- thin border so the overlay region
# is clearly delineated, even in dense text.
_SEVERITY_OVERLAY_PENS = {
    sev: QPen(QColor(c.red(), c.green(), c.blue(), 120), 1)
    for sev, c in _SEVERITY_OVERLAY_COLORS.items()
}
_DEFAULT_OVERLAY_PEN = QPen(QColor(120, 120, 120, 120), 1)

# Find bar: every match tinted yellow, the current one filled deeper and
# outlined in orange -- as a browser's Find does. (Green is the matched
# text's color.)
_SEARCH_BRUSH = QBrush(QColor(255, 214, 0, 95))
_SEARCH_PEN = QPen(Qt.PenStyle.NoPen)
_SEARCH_CURRENT_BRUSH = QBrush(QColor(255, 170, 0, 140))
_SEARCH_CURRENT_PEN = QPen(QColor(230, 110, 0), 2)
# Text found the same in both documents (View > Show Matched Text): a very
# light green under everything else, so the differences stand out on it.
_MATCHED_BRUSH = QBrush(QColor(34, 197, 94, 34))
_MATCHED_PEN = QPen(Qt.PenStyle.NoPen)
_SEARCH_CURRENT_PEN.setCosmetic(True)
# Table cells as the comparison detected them (View > Show Table Cells):
# blue for a table read cell by cell, purple for a table drawn inside a
# cell of another (read cell by cell there), grey dashes for a grid that
# was found but is read as ordinary text (ruling lines over a paragraph).
_TABLE_COLORS = {"table": QColor(2, 132, 199), "nested": QColor(192, 38, 211), "ignored": QColor(120, 128, 140)}
_TABLE_LABELS = {"table": "Table", "nested": "Nested table", "ignored": "Read as text"}
# Pages of table layout read per step (each step is drawn as it arrives).
_TABLE_LAYOUT_PAGES_PER_STEP = 6

# Typing pauses this long (ms) before the document is searched.
_SEARCH_DELAY_MS = 180
# Matches tinted at once; past this the count still covers them all.
_MAX_TINTED_MATCHES = 5000

# Each Ctrl+wheel notch (or gesture update) multiplies the zoom factor
# by this much -- chosen to match the feel of the toolbar +/- buttons
# (ZOOM_STEP = 1.25 in main.py) without jumping too fast on a pinch.
_WHEEL_ZOOM_STEP = 0.15

# Layout constants for render_synthesized's plain text-flow preview
# (DOCX/PPTX/XLSX -- see that method's docstring for why this is a
# deliberate simplification, not a faithful per-format renderer).
# _SYNTH_PAGE_WIDTH_PT matches core.document_extractor's own
# _DOCX_PAGE_WIDTH_PT so a DOCX's synthesized Word coordinates always
# fit within the drawn page width.
_SYNTH_PAGE_WIDTH_PT = 612.0
_SYNTH_PAGE_MARGIN_PT = 36.0  # bottom margin added past the last word
_SYNTH_MIN_PAGE_HEIGHT_PT = 200.0  # a page with little/no content still gets a visible box
_SYNTH_FONT_PT = 9.0
_SYNTH_BG_COLOR = QColor(255, 255, 255)
_SYNTH_TEXT_COLOR = QColor(20, 20, 20)


def _draw_synthesized_page(words: list, width_px: int, height_px: int) -> QPixmap:
    """Render one synthesized page's Words onto a QPixmap using
    QPainter, at RENDER_DPI_SCALE -- the same scale `_scene_rects_for`
    assumes when it maps a Word's PDF-point bbox to scene pixels, so a
    highlight drawn later lands exactly on the glyphs painted here.

    core.document_extractor lays words out using a rough, Qt-free
    per-character width ESTIMATE (it has no font metrics to draw on),
    so the box it assigns a word essentially never matches this font's
    REAL rendered width for that text -- worst on all-caps/bold
    headings, where real glyphs run wide enough to bleed into the next
    word's box with no visible gap at all (e.g. a synthesized "HIGHLIGHTS
    OF PRESCRIBING INFORMATION" heading rendering as the unreadable
    "HIGHLIGHTSOF PRESCRIBINGINFORMATION"). Rather than trying to make
    the estimate exact -- futile, since it depends on the OS/font/DPI
    actually used to paint -- each word's glyphs are horizontally
    scaled to fit EXACTLY inside its own already-assigned box, measured
    with this very font via QFontMetricsF. That is what the highlighter
    draws around, so words can never visually overlap/merge regardless
    of how far off the layout estimate is on any given machine.
    """
    pixmap = QPixmap(max(width_px, 1), max(height_px, 1))
    pixmap.fill(_SYNTH_BG_COLOR)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont()
        font.setPointSizeF(_SYNTH_FONT_PT)
        painter.setFont(font)
        painter.setPen(_SYNTH_TEXT_COLOR)
        metrics = QFontMetricsF(font)
        painter.scale(RENDER_DPI_SCALE, RENDER_DPI_SCALE)
        for w in words:
            if not w.text:
                continue
            # Baseline roughly 80% down the word's own box -- QPainter
            # draws text from its baseline, not its top-left, so this
            # keeps glyphs vertically centered in the box the diff
            # highlighter will later draw around.
            baseline_y = w.top + (w.bottom - w.top) * 0.8
            box_width = max(w.x1 - w.x0, 0.1)
            measured_width = metrics.horizontalAdvance(w.text) or box_width
            painter.save()
            painter.translate(w.x0, baseline_y)
            painter.scale(box_width / measured_width, 1.0)
            painter.drawText(0, 0, w.text)
            painter.restore()
    finally:
        painter.end()
    return pixmap


class _PageRenderer(QObject):
    """Renders PDF pages to images on a background thread, so drawing a
    page never blocks scrolling or navigation. The thread owns its own
    PyMuPDF document (documents aren't shared between threads)."""

    rendered = Signal(int, int, float, QImage)  # generation, page index, resolution, image

    def __init__(self) -> None:
        super().__init__()
        self._requests: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, name="page-renderer", daemon=True).start()

    def request(self, generation: int, path: str, index: int, resolution: float) -> None:
        self._requests.put((generation, path, index, resolution))

    def _run(self) -> None:
        doc, doc_path = None, None
        while True:
            generation, path, index, resolution = self._requests.get()
            try:
                if path != doc_path:
                    if doc is not None:
                        doc.close()
                    doc, doc_path = fitz.open(path), path
                scale = RENDER_DPI_SCALE * resolution
                pix = doc.load_page(index).get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                image = QImage(
                    pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888
                ).copy()  # own the pixels; `pix` is freed below
            except Exception:  # noqa: BLE001 -- a page that can't be drawn stays blank
                logger.exception("Could not draw page %s of %s", index + 1, path)
                doc, doc_path = None, None
                continue
            self.rendered.emit(generation, index, resolution, image)


class _TextIndexer(QObject):
    """Reads a document's words for the Find bar on a background thread
    (a long PDF takes a moment), so opening a document never waits."""

    indexed = Signal(int, object)  # generation, core.text_search.SearchIndex (None on failure)

    def __init__(self) -> None:
        super().__init__()
        self._requests: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, name="text-indexer", daemon=True).start()

    def request(self, generation: int, path: str, kind: str) -> None:
        self._requests.put((generation, path, kind))

    def _run(self) -> None:
        from core.text_search import build_search_index

        while True:
            request = self._requests.get()
            while not self._requests.empty():  # only the newest document matters
                request = self._requests.get()
            generation, path, kind = request
            try:
                index = build_search_index(path, kind)
            except Exception:  # noqa: BLE001 -- the Find bar just finds nothing
                logger.exception("Could not read the text of %s for searching", path)
                index = None
            self.indexed.emit(generation, index)


class _TableLayoutReader(QObject):
    """Reads which tables and cells the comparison detects on each page
    (`core.pdf_extractor.extract_words(table_layout=...)`) on a
    background thread, a few pages at a time, each step sent as it is
    done -- so the outlines fill in without holding up the viewer."""

    ready = Signal(int, object)  # request number, {page: [table dicts]}

    def __init__(self) -> None:
        super().__init__()
        self._requests: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, name="table-layout", daemon=True).start()

    def request(self, number: int, path: str, crop: dict | None = None) -> None:
        self._requests.put((number, path, crop))

    def _run(self) -> None:
        from core.pdf_extractor import extract_words

        while True:
            generation, path, crop = self._requests.get()
            try:
                with fitz.open(path) as doc:
                    count = doc.page_count
                for first in range(1, count + 1, _TABLE_LAYOUT_PAGES_PER_STEP):
                    if not self._requests.empty():
                        break  # another document was opened meanwhile
                    layout: dict[int, list[dict]] = {}
                    last = min(first + _TABLE_LAYOUT_PAGES_PER_STEP - 1, count)
                    extract_words(path, first_page=first, last_page=last, table_layout=layout, crop=crop or None)
                    self.ready.emit(generation, layout)
            except Exception:  # noqa: BLE001 -- the outlines just stay off
                logger.exception("Could not read the table layout of %s", path)


class _SearchField(QLineEdit):
    """The Find bar's text box: Enter / Shift+Enter step through the
    matches, Esc clears the search. These keys are claimed before the
    window's shortcuts (Esc there clears drawn areas)."""

    next_requested = Signal()
    previous_requested = Signal()
    escaped = Signal()
    focused = Signal()

    _KEYS = (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ShortcutOverride and event.key() in self._KEYS:
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 -- Qt override
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.previous_requested.emit()
            else:
                self.next_requested.emit()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.escaped.emit()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802 -- Qt override
        super().focusInEvent(event)
        self.focused.emit()


class _ZoomableGraphicsView(QGraphicsView):
    """QGraphicsView that turns trackpad pinch + Ctrl+wheel into zoom,
    and reports clicks (as opposed to drags).

    A two-finger pinch on a trackpad arrives as a QPinchGesture (Windows
    precision touchpads, macOS trackpads); many other touchpads deliver
    the same gesture as a Ctrl+wheel scroll. Both are mapped to a
    RELATIVE zoom multiplier ("1.1" zooms in, "0.9" zooms out) plus the
    scene point to keep fixed while zooming, emitted as
    ``zoom_requested``. Plain wheel scrolling is left untouched.
    """

    zoom_requested = Signal(float, QPointF)  # relative multiplier (>1 zooms in), scene anchor
    clicked = Signal(QPointF)  # scene position of a left click that wasn't a drag
    double_clicked = Signal(QPointF)  # scene position of a left double-click
    interacted = Signal()  # any mouse/wheel use of this view
    file_dropped = Signal(str)  # a single supported document dropped onto the view
    files_dropped = Signal(list)  # multiple supported documents dropped onto the view
    # A rectangle drawn with Shift+drag (or a plain drag in area mode),
    # in scene coordinates; the flag is True when Ctrl was held too,
    # adding the rectangle to the selection instead of replacing it.
    area_dragged = Signal(QRectF, bool)

    def __init__(self, scene: QGraphicsScene, parent: QWidget | None = None) -> None:
        super().__init__(scene, parent)
        # Without this, Qt never delivers QPinchGesture to this widget.
        self.grabGesture(Qt.GestureType.PinchGesture)
        self._press_pos: QPointF | None = None
        self.setAcceptDrops(True)
        # Area selection: a plain drag draws a rectangle instead of
        # panning while this is on; Shift+drag always does.
        self.area_mode = False
        self._area_start: QPointF | None = None
        self._area_add = False
        self._area_item: QGraphicsRectItem | None = None

    @staticmethod
    def _dropped_paths(event) -> list[str]:
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        paths = []
        for url in urls:
            if url.isLocalFile():
                path = url.toLocalFile()
                if path.lower().endswith(_OPENABLE_EXTENSIONS):
                    paths.append(path)
        return paths

    def dragEnterEvent(self, event) -> None:
        if self._dropped_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._dropped_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        paths = self._dropped_paths(event)
        if len(paths) == 1:
            event.acceptProposedAction()
            self.file_dropped.emit(paths[0])
        elif len(paths) > 1:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Gesture:
            return self._handle_gesture_event(event)
        return super().event(event)

    def _handle_gesture_event(self, event: QGestureEvent) -> bool:
        gesture = event.gesture(Qt.GestureType.PinchGesture)
        if gesture is None:
            return super().event(event)
        # scaleFactor() is relative to the previous update, so a steady
        # pinch accumulates smoothly into the panel's zoom factor.
        factor = gesture.scaleFactor()
        if factor != 1.0:
            viewport_pos = self.viewport().mapFromGlobal(gesture.centerPoint().toPoint())
            if self.viewport().rect().contains(viewport_pos):
                anchor = self.mapToScene(viewport_pos)
            else:
                anchor = self.mapToScene(self.viewport().rect().center())
            self.interacted.emit()
            self.zoom_requested.emit(float(factor), QPointF(anchor))
        event.accept()
        return True

    def wheelEvent(self, event) -> None:
        self.interacted.emit()
        # Touchpads that don't emit QPinchGesture report a pinch as
        # Ctrl+wheel; without the modifier, wheel stays plain scrolling.
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                anchor = self.mapToScene(event.position().toPoint())
                # Exponential, so one notch in and one notch out cancel out.
                self.zoom_requested.emit((1.0 + _WHEEL_ZOOM_STEP) ** (delta / 120.0), QPointF(anchor))
                event.accept()
                return
        super().wheelEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.interacted.emit()
            modifiers = event.modifiers()
            if self.area_mode or modifiers & Qt.KeyboardModifier.ShiftModifier:
                self._start_area(event, bool(modifiers & Qt.KeyboardModifier.ControlModifier))
                event.accept()
                return
            self._press_pos = event.position()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._area_start is not None:
            current = self.mapToScene(event.position().toPoint())
            if self._area_item is not None:
                self._area_item.setRect(QRectF(self._area_start, current).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def _start_area(self, event, add: bool) -> None:
        self._area_start = QPointF(self.mapToScene(event.position().toPoint()))
        self._area_add = add
        accent = QColor(theme.tokens().accent)
        pen = QPen(accent, 1.5, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        fill = QColor(accent)
        fill.setAlpha(30)
        self._area_item = self.scene().addRect(QRectF(self._area_start, self._area_start), pen, QBrush(fill))
        self._area_item.setZValue(5)

    def _finish_area(self, event) -> None:
        start, self._area_start = self._area_start, None
        end = QPointF(self.mapToScene(event.position().toPoint()))
        if self._area_item is not None:
            self.scene().removeItem(self._area_item)
            self._area_item = None
        rect = QRectF(start, end).normalized()
        # A click without a drag selects nothing.
        if rect.width() >= 4 and rect.height() >= 4:
            self.area_dragged.emit(rect, self._area_add)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.interacted.emit()
            self.double_clicked.emit(QPointF(self.mapToScene(event.position().toPoint())))
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._area_start is not None and event.button() == Qt.MouseButton.LeftButton:
            self._finish_area(event)
            event.accept()
            return
        super().mouseReleaseEvent(event)
        press, self._press_pos = self._press_pos, None
        if (
            event.button() == Qt.MouseButton.LeftButton
            and press is not None
            and (event.position() - press).manhattanLength() < _CLICK_SLOP
        ):
            self.clicked.emit(QPointF(self.mapToScene(event.position().toPoint())))


class DocumentPanel(QWidget):
    """Self-contained load-and-preview panel with a zoomable, scrollable,
    multi-page view.

    Args:
        title: Heading shown above the panel, e.g. "Original" or "Suspect".

    Signals:
        view_moved: The user scrolled this panel (not emitted for
            programmatic moves), so main.py can keep a locked partner
            in step.
        zoom_changed: The user changed this panel's zoom (toolbar,
            pinch, Ctrl+wheel); carries the new absolute factor.
        interacted: The user clicked, scrolled or zoomed in this panel.
        point_clicked: A click (not a drag) on a page, as
            (page, x, y) in PDF points.
        point_double_clicked: A double-click on a page, same
            coordinates -- main.py rematches the difference there.
        loaded: Emitted with the file path once a document has rendered
            successfully, so main.py knows when it's safe to enable
            the Compare button.
    """

    view_moved = Signal()
    zoom_changed = Signal(float)
    interacted = Signal()
    point_clicked = Signal(int, float, float)
    # A double-click on a page, as (page, x, y) in PDF points: the
    # reviewer asking for whatever is there to be rematched.
    point_double_clicked = Signal(int, float, float)
    # The area drawn on this document changed (see `area_selection`):
    # the text a reviewer picked to rematch by hand.
    area_selected = Signal()
    loaded = Signal(str)
    # Emitted with (merged_path, should_compare) when multiple files are merged and loaded
    merge_completed = Signal(str, bool)
    # Emitted with the file path when a document starts loading in the
    # background (a Word document being laid out); `file_path` is None
    # until the matching `loaded`.
    loading = Signal(str)

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.file_path: str | None = None
        self.source_files: list[str] | None = None
        self.source_file_infos: list[dict] | None = None
        self.can_compare: bool = False
        self._pixmap_item = None  # page 1's item; None while no document is shown
        self._base_pixmap: QPixmap | None = None  # unused; kept for compatibility
        # PDF shown (drawn page by page in the background) and the pages
        # currently drawn from it.
        self._doc_path: str | None = None
        self._generation = 0  # bumps per document, so stale images are ignored
        self._drawn_pages: dict[int, tuple[QGraphicsPixmapItem, float]] = {}  # index -> (item, resolution)
        self._pending_pages: set[tuple[int, float]] = set()
        self._renderer = _PageRenderer()
        self._renderer.rendered.connect(self._on_page_rendered)
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(0)
        self._render_timer.timeout.connect(self._draw_next_visible_page)
        self._zoom_factor: float = 1.0
        self._syncing_scroll = False  # guard against feedback loops
        self._highlight_items: list[QGraphicsRectItem] = []
        self._missing_items: list[QGraphicsRectItem] = []
        self._overlay_items: list[QGraphicsRectItem] = []  # persistent severity overlays
        self._matched_items: list[QGraphicsRectItem] = []  # text matched in both documents
        self._crop_overlay_items: list[QGraphicsRectItem] = []  # crop region overlays
        # (scene rect, DiffItem) for every overlay, to find the
        # difference under a click.
        self._overlay_hits: list[tuple[QRectF, object]] = []
        # The area drawn to rematch by hand: (page, (x0, top, x1, bottom))
        # in PDF points, and the outlines showing it.
        self._area: list[tuple[int, tuple[float, float, float, float]]] = []
        self._area_items: list[QGraphicsRectItem] = []
        # Find bar: the document's words (read in the background), the
        # matches for the current query, which one is current, and the
        # tints drawn for them.
        self._indexer = _TextIndexer()
        self._indexer.indexed.connect(self._on_indexed)
        self._search_index = None
        self._indexing = False
        self._search_matches: list = []
        self._search_current = -1
        self._search_items: list[QGraphicsRectItem] = []
        self._search_current_items: list[QGraphicsRectItem] = []
        self._search_navigate_when_ready = False
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(_SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(lambda: self._run_search(navigate=True))
        # Table cells overlay: the layout read so far ({page: [table]}),
        # whether it is shown, and the outlines drawn.
        self._layout_reader = _TableLayoutReader()
        self._layout_reader.ready.connect(self._on_table_layout)
        self._table_layout: dict[int, list[dict]] = {}
        # What the layout shown was read for: (document generation, crop);
        # each request is numbered so a late step of an old one is ignored.
        self._table_layout_for: tuple | None = None
        self._layout_request = 0
        self._crop: dict = {}
        self._tables_visible = False
        self._table_items: list = []

        # --- Multi-page state ---
        # One Y offset (in unscaled scene pixels) per page, in page order,
        # so a (page, bbox) pair from core/diff_engine.py can be placed
        # directly onto the single continuous scene.
        self._page_y_offsets: list[float] = []
        self._page_count: int = 0
        self._current_page: int = 1  # 1-indexed "page currently in view"

        # DOCX conversion in progress: the file being converted, and the
        # worker (kept referenced so its signals object stays alive).
        self._converting_path: str | None = None
        self._convert_worker: ConvertWorker | None = None

        self._title = title
        self._page_rects: list[QRectF] = []
        self._page_border_items: list[QGraphicsRectItem] = []
        self._message_item = None

        # --- header: role, file name, details, Open, Merge ---
        header = QWidget()
        header.setObjectName("panelHeader")
        self.title_label = QLabel(title.upper())
        self.title_label.setObjectName("panelTitle")
        if title.lower() in ("original", "suspect"):
            # Colored role tag: blue Original, teal Suspect (see ui/theme.py).
            self.title_label.setProperty("role", title.lower())
        self.title_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.file_label = QLabel("No document")
        self.file_label.setObjectName("fileName")
        self.file_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._file_text = "No document"
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("muted")
        self.load_button = QPushButton("Open\u2026")
        self.load_button.setToolTip(f"Open the {title} document (PDF, Word, PowerPoint or Excel)")
        self.load_button.clicked.connect(self.on_load_clicked)
        self.merge_button = QPushButton("Merge\u2026")
        self.merge_button.setToolTip(f"Merge multiple split files into the {title} document")
        self.merge_button.clicked.connect(lambda: self.on_merge_clicked())
        for button, name in ((self.load_button, "folder"), (self.merge_button, "merge")):
            button.setIconSize(QSize(15, 15))
            icons.bind(button, name)
        text_column = QVBoxLayout()
        text_column.setSpacing(1)
        text_column.addWidget(self.title_label)
        text_column.addWidget(self.file_label)
        text_column.addWidget(self.meta_label)
        btn_widget = QWidget()
        btn_layout = QHBoxLayout(btn_widget)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(6)
        btn_layout.addWidget(self.load_button)
        btn_layout.addWidget(self.merge_button)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 8, 12, 8)
        header_layout.addLayout(text_column, stretch=1)
        header_layout.addWidget(btn_widget, alignment=Qt.AlignmentFlag.AlignVCenter)

        # --- navigation strip: pages and zoom ---
        nav = QWidget()
        nav.setObjectName("panelNav")
        self.prev_page_button = self._flat_button("\u2039", "Previous page")
        self.prev_page_button.setProperty("nav", True)
        self.prev_page_button.clicked.connect(self.go_to_previous_page)
        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, 1)
        self.page_spin.setFixedWidth(52)
        self.page_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.setToolTip("Go to page")
        self.page_spin.valueChanged.connect(self._on_page_spin_changed)
        self.page_label = QLabel("of \u2013")
        self.page_label.setObjectName("muted")
        self.page_label.setMinimumWidth(40)
        self.next_page_button = self._flat_button("\u203a", "Next page")
        self.next_page_button.setProperty("nav", True)
        self.next_page_button.clicked.connect(self.go_to_next_page)

        self.zoom_out_button = self._flat_button("\u2212", "Zoom out this document (Ctrl+\u2212)")
        self.zoom_out_button.clicked.connect(lambda: self.zoom_by(1 / ZOOM_STEP))
        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("muted")
        self.zoom_label.setMinimumWidth(42)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_in_button = self._flat_button("+", "Zoom in this document (Ctrl++)")
        self.zoom_in_button.clicked.connect(lambda: self.zoom_by(ZOOM_STEP))
        self.fit_button = self._flat_button("Fit width", "Fit the page width to this viewer (Ctrl+0)")
        self.fit_button.clicked.connect(self._on_fit_clicked)

        for button, name in (
            (self.prev_page_button, "chevron-left"), (self.next_page_button, "chevron-right"),
            (self.zoom_out_button, "zoom-out"), (self.zoom_in_button, "zoom-in"),
        ):
            button.setText("")
            button.setProperty("nav", False)
            button.setIconSize(QSize(16, 16))
            icons.bind(button, name)
        self.fit_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.fit_button.setIconSize(QSize(16, 16))
        icons.bind(self.fit_button, "fit-width")
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(8, 3, 8, 3)
        nav_layout.setSpacing(4)
        nav_layout.addWidget(self.prev_page_button)
        nav_layout.addWidget(self.page_spin)
        nav_layout.addWidget(self.page_label)
        nav_layout.addWidget(self.next_page_button)
        nav_layout.addStretch()
        nav_layout.addWidget(self.zoom_out_button)
        nav_layout.addWidget(self.zoom_label)
        nav_layout.addWidget(self.zoom_in_button)
        nav_layout.addWidget(self.fit_button)

        # --- find bar: search this document's text ---
        search_bar = QWidget()
        search_bar.setObjectName("panelNav")
        self.search_field = _SearchField()
        self.search_field.setPlaceholderText(f"Search the {title} document\u2026")
        self.search_field.setClearButtonEnabled(True)
        self.search_field.setToolTip(
            "Find a word or sentence in this document (Ctrl+Shift+F).\n"
            "Enter: next match \u00b7 Shift+Enter: previous \u00b7 Esc: clear"
        )
        self.search_field.textChanged.connect(self._on_search_text_changed)
        self.search_field.next_requested.connect(self.find_next)
        self.search_field.previous_requested.connect(self.find_previous)
        self.search_field.escaped.connect(self._on_search_escaped)
        self.search_field.focused.connect(self.interacted)
        self.search_count_label = QLabel("")
        self.search_count_label.setObjectName("muted")
        self.search_count_label.setMinimumWidth(64)
        self.search_count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.search_prev_button = self._flat_button("\u2191", "Previous match (Shift+Enter, Shift+F3)")
        self.search_prev_button.clicked.connect(self.find_previous)
        self.search_next_button = self._flat_button("\u2193", "Next match (Enter, F3)")
        self.search_next_button.clicked.connect(self.find_next)
        self.match_case_button = self._flat_button("Aa", "Match case")
        self.match_case_button.setCheckable(True)
        self.match_case_button.toggled.connect(lambda _on: self._run_search(navigate=True))
        self.whole_word_button = self._flat_button("Whole word", "Match whole words only")
        self.whole_word_button.setCheckable(True)
        self.whole_word_button.toggled.connect(lambda _on: self._run_search(navigate=True))
        search_icon = self.search_field.addAction(icons.icon("search", "muted"), QLineEdit.ActionPosition.LeadingPosition)
        theme.manager().changed.connect(lambda: search_icon.setIcon(icons.icon("search", "muted")))
        for button, name in ((self.search_prev_button, "chevron-up"), (self.search_next_button, "chevron-down")):
            button.setText("")
            button.setIconSize(QSize(16, 16))
            icons.bind(button, name)
        search_layout = QHBoxLayout(search_bar)
        search_layout.setContentsMargins(8, 3, 8, 3)
        search_layout.setSpacing(4)
        search_layout.addWidget(self.search_field, stretch=1)
        search_layout.addWidget(self.search_count_label)
        search_layout.addWidget(self.search_prev_button)
        search_layout.addWidget(self.search_next_button)
        search_layout.addWidget(self.match_case_button)
        search_layout.addWidget(self.whole_word_button)

        self.layout_note = QLabel()
        self.layout_note.setObjectName("note")
        self.layout_note.setWordWrap(True)
        self.layout_note.setContentsMargins(12, 4, 12, 4)
        self.layout_note.hide()

        self.scene = QGraphicsScene(self)
        self.view = _ZoomableGraphicsView(self.scene)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        # Allow the view to flex down vertically (to 60px) so the user can freely
        # expand the bottom deviations dock via cursor drag without being blocked
        # by a rigid minimum central widget size.
        self.view.setMinimumSize(160, 60)
        self.view.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        self.view.horizontalScrollBar().valueChanged.connect(self._on_scrolled)
        self.view.zoom_requested.connect(self._on_view_zoom_requested)
        self.view.interacted.connect(self.interacted)
        self.view.clicked.connect(self._on_view_clicked)
        self.view.double_clicked.connect(self._on_view_double_clicked)
        self.view.area_dragged.connect(self._on_area_dragged)
        self.view.file_dropped.connect(self.load_document)
        self.view.files_dropped.connect(lambda paths: self.on_merge_clicked(initial_files=paths))

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(nav)
        layout.addWidget(search_bar)
        layout.addWidget(self.layout_note)
        layout.addWidget(self.view, stretch=1)
        self.setLayout(layout)

        theme.manager().changed.connect(self._apply_theme)
        self._apply_theme()
        self._show_empty_state()
        self._update_page_controls()
        self._update_search_controls()

    # Folder the next Open dialog starts in (shared by both panels).
    last_dir = ""

    @staticmethod
    def _flat_button(text: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tooltip)
        button.setProperty("flat", True)
        button.setAutoRaise(True)
        return button

    def _show_empty_state(self) -> None:
        """No document yet: a drop zone saying what can be opened."""
        self._show_message("")
        t = theme.tokens()
        width, height = 400.0, 220.0
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, width, height), 14, 14)
        pen = QPen(QColor(t.border_strong), 1.4, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        self.scene.addPath(path, pen, QBrush(QColor(t.surface)))
        ratio = max(self.devicePixelRatioF(), 1.0)
        art = self.scene.addPixmap(icons.pixmap("upload", round(40 * ratio), "accent"))
        art.setScale(1 / ratio)
        art.setPos((width - 40) / 2, 38)

        def centred(text: str, y: float, size: float, color: str, bold: bool = False) -> None:
            font = QFont()
            font.setPointSizeF(size)
            font.setBold(bold)
            item = self.scene.addText(text, font)
            item.setDefaultTextColor(QColor(color))
            item.setPos((width - item.boundingRect().width()) / 2, y)

        centred(f"Drop the {self._title} document here", 92, 11.5, t.text, bold=True)
        centred("PDF, Word, PowerPoint or Excel \u2014 or click Open\u2026", 124, 9, t.muted)
        centred("Several files at once are merged into one document", 148, 8.5, t.muted)
        self._empty_state = True
        self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-24, -24, 24, 24))

    def _apply_theme(self) -> None:
        t = theme.tokens()
        if getattr(self, "_empty_state", False) and self._page_count == 0:
            self._show_empty_state()
        self.view.setBackgroundBrush(QColor(t.canvas))
        pen = QPen(QColor(t.page_border), 1)
        pen.setCosmetic(True)
        for item in self._page_border_items:
            item.setPen(pen)
        if self._message_item is not None:
            self._message_item.setDefaultTextColor(QColor(t.muted))

    def _set_file_info(self, name: str | None, details: str = "") -> None:
        self._file_text = name or "No document"
        self.file_label.setToolTip(name or "")
        self.meta_label.setText(details)
        self._elide_file_label()

    def _elide_file_label(self) -> None:
        metrics = self.file_label.fontMetrics()
        width = max(self.file_label.width(), 40)
        self.file_label.setText(metrics.elidedText(self._file_text, Qt.TextElideMode.ElideMiddle, width))

    def resizeEvent(self, event) -> None:  # noqa: N802 -- Qt override
        super().resizeEvent(event)
        self._elide_file_label()
        self._render_timer.start()

    def _add_page_frame(self, width: float, height: float, y: float) -> QGraphicsRectItem:
        """A blank white page with a 1px border at scene Y ``y``."""
        rect = QRectF(0, y, width, height)
        pen = QPen(QColor(theme.tokens().page_border), 1)
        pen.setCosmetic(True)
        border = self.scene.addRect(rect.adjusted(-1, -1, 1, 1), pen, QBrush(QColor(255, 255, 255)))
        border.setZValue(-1)
        self._page_border_items.append(border)
        self._page_rects.append(rect)
        return border

    def _add_page(self, pixmap: QPixmap, y: float) -> QGraphicsPixmapItem:
        """Place one rendered page at scene Y ``y`` with a 1px border."""
        self._add_page_frame(pixmap.width(), pixmap.height(), y)
        item = self.scene.addPixmap(pixmap)
        item.setPos(0, y)
        return item

    def _close_doc(self) -> None:
        self._render_timer.stop()
        self._doc_path = None
        self._generation += 1
        self._drawn_pages = {}
        self._pending_pages = set()

    def _draw_next_visible_page(self) -> None:
        """Request images for the pages on (or next to) the screen that
        aren't drawn at the resolution the zoom needs, nearest first."""
        if self._doc_path is None or not self._page_rects:
            return
        visible = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        # Also prepare the page above and below what's on screen.
        nearby = visible.adjusted(0, -visible.height(), 0, visible.height())
        center_y = visible.center().y()
        wanted = sorted(
            (i for i, rect in enumerate(self._page_rects) if rect.intersects(nearby)),
            key=lambda i: abs(self._page_rects[i].center().y() - center_y),
        )
        resolution = self._resolution_for_zoom()
        missing = [
            i for i in wanted
            if (i not in self._drawn_pages or self._drawn_pages[i][1] < resolution)
            and (i, resolution) not in self._pending_pages
        ]
        if not missing:
            if not self._pending_pages:
                self._release_far_pages(set(wanted))
            return
        for index in missing[: max(0, _MAX_PENDING_PAGES - len(self._pending_pages))]:
            self._pending_pages.add((index, resolution))
            self._renderer.request(self._generation, self._doc_path, index, resolution)

    def _on_page_rendered(self, generation: int, index: int, resolution: float, image: QImage) -> None:
        if generation != self._generation:
            return  # a document that's no longer shown
        self._pending_pages.discard((index, resolution))
        current = self._drawn_pages.get(index)
        if current is None or current[1] < resolution:
            item = self.scene.addPixmap(QPixmap.fromImage(image))
            item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            rect = self._page_rects[index]
            item.setScale(rect.width() / max(image.width(), 1))
            item.setPos(rect.left(), rect.top())
            if current is not None:
                self.scene.removeItem(current[0])
            self._drawn_pages[index] = (item, resolution)
        self._render_timer.start()  # request the next pages

    def _release_far_pages(self, keep: set[int]) -> None:
        if len(self._drawn_pages) <= _MAX_DRAWN_PAGES:
            return
        center_y = self.view.mapToScene(self.view.viewport().rect().center()).y()
        far = sorted(
            (i for i in self._drawn_pages if i not in keep),
            key=lambda i: -abs(self._page_rects[i].center().y() - center_y),
        )
        for index in far[: len(self._drawn_pages) - _MAX_DRAWN_PAGES]:
            self.scene.removeItem(self._drawn_pages.pop(index)[0])

    def _resolution_for_zoom(self) -> float:
        needed = self._zoom_factor * self.view.devicePixelRatioF()
        for step in _RESOLUTION_STEPS:
            if needed <= step * 1.25:
                return step
        return _RESOLUTION_STEPS[-1]

    def _finish_pages(self) -> None:
        """Scene rect = all pages plus a margin of empty canvas."""
        bounds = QRectF()
        for rect in self._page_rects:
            bounds = bounds.united(rect)
        self.scene.setSceneRect(
            bounds.adjusted(-_CANVAS_MARGIN, -_CANVAS_MARGIN, _CANVAS_MARGIN, _CANVAS_MARGIN)
        )

    def _reset_scene(self) -> None:
        self._close_doc()
        self.scene.clear()
        self._highlight_items = []  # scene.clear() already deleted the old items
        self._missing_items = []
        self._overlay_items = []
        self._overlay_hits = []
        self._matched_items = []
        self._crop_overlay_items = []
        had_area = bool(self._area)
        self._area = []
        self._area_items = []
        # scene.clear() deleted the tints and table outlines; a new
        # document is searched and analyzed anew.
        self._table_items = []
        self._table_layout = {}
        self._table_layout_for = None
        self._crop = {}  # a new document: main.py hands over its own crop
        self._search_items = []
        self._search_current_items = []
        self._search_matches = []
        self._search_current = -1
        self._search_index = None
        self._indexing = False
        self._page_y_offsets = []
        self._page_rects = []
        if had_area:
            self.area_selected.emit()
        self._page_border_items = []
        self._message_item = None

    # ------------------------------------------------------------------
    # Loading / rendering
    # ------------------------------------------------------------------

    def on_load_clicked(self) -> None:
        """Open a file dialog and load the chosen document(s)."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, f"Open {self._title} document(s)", DocumentPanel.last_dir, _OPEN_FILTER
        )
        if paths:
            DocumentPanel.last_dir = os.path.dirname(paths[0])
            if len(paths) == 1:
                self.load_document(paths[0])
            else:
                self.on_merge_clicked(initial_files=paths)

    def on_merge_clicked(
        self,
        initial_files: list[str] | None = None,
        can_compare: bool | None = None,
    ) -> None:
        """Open the merge dialog to combine multiple split files into this document."""
        from PySide6.QtWidgets import QDialog
        from ui.merge_dialog import MergeDocumentsDialog

        if can_compare is None:
            can_compare = self.can_compare

        dialog = MergeDocumentsDialog(
            parent=self,
            initial_files=initial_files,
            title=self._title,
            can_compare=can_compare,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.merged_path:
            self.load_merged_document(
                dialog.merged_path,
                source_files=dialog.source_files,
                file_infos=dialog.file_infos,
            )
            self.merge_completed.emit(dialog.merged_path, dialog.should_compare)

    def load_merged_document(
        self,
        merged_path: str,
        source_files: list[str],
        file_infos: list[dict] | None = None,
    ) -> None:
        """Load a merged document, keeping track of its source constituent files."""
        self.source_files = list(source_files)
        self.source_file_infos = file_infos or []
        self.render_pdf(merged_path)

    def load_document(self, path: str) -> None:
        """Show ``path`` in this panel; emits `loaded` once it's shown.

        PDFs render as real rasterized pages (`render_pdf`). DOCX files
        are converted to PDF by Microsoft Word/LibreOffice in the
        background and then rendered the same way. PPTX/XLSX get the
        synthesized preview (`render_synthesized`).
        """
        from core.document_extractor import document_kind
        from core.office_convert import CONVERTIBLE_EXTENSIONS

        self.source_files = None
        self.source_file_infos = None
        self._converting_path = None  # a newer load supersedes any pending conversion
        try:
            kind = document_kind(path)
            if kind == "pdf":
                self.render_pdf(path)
            elif os.path.splitext(path)[1].lower() in CONVERTIBLE_EXTENSIONS:
                self._start_conversion(path)
            else:
                self.render_synthesized(path)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            logger.exception("Failed to render document")
            QMessageBox.critical(self, "Could not open file", str(exc))

    def _start_conversion(self, path: str) -> None:
        self._show_message("Laying out the document with Microsoft Word\u2026")
        self._set_file_info(os.path.basename(path), "Word document \u00b7 laying out\u2026")
        self._converting_path = path
        self.file_path = None
        self.loading.emit(path)
        worker = ConvertWorker(path)
        worker.signals.converted.connect(self._on_converted)
        worker.signals.unavailable.connect(self._on_converter_unavailable)
        worker.signals.failed.connect(self._on_conversion_failed)
        self._convert_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _on_converted(self, source_path: str, pdf_path: str) -> None:
        if source_path != self._converting_path:
            return  # another file was loaded meanwhile
        self._converting_path = None
        try:
            self.render_pdf(pdf_path, source_path=source_path)
        except RuntimeError as exc:
            logger.exception("Failed to render converted document")
            self._show_message("")
            QMessageBox.critical(self, "Could not open file", str(exc))

    def _on_converter_unavailable(self, source_path: str) -> None:
        if source_path != self._converting_path:
            return
        self._converting_path = None
        try:
            self.render_synthesized(source_path)
        except RuntimeError as exc:
            self._show_message("")
            QMessageBox.critical(self, "Could not open file", str(exc))

    def _on_conversion_failed(self, source_path: str, message: str) -> None:
        if source_path != self._converting_path:
            return
        self._converting_path = None
        self._show_message(f"Could not open {os.path.basename(source_path)}.")
        self._set_file_info(None)
        QMessageBox.critical(self, "Could not open file", message)

    def _show_message(self, text: str) -> None:
        """Replace the view's content with a centered status message."""
        self._empty_state = False
        self._reset_scene()
        self._page_count = 0
        self._pixmap_item = None
        self._base_pixmap = None
        self.layout_note.hide()
        if text:
            font = QFont()
            font.setPointSizeF(10.5)
            item = self.scene.addText(text, font)
            option = item.document().defaultTextOption()
            option.setAlignment(Qt.AlignmentFlag.AlignCenter)
            item.document().setDefaultTextOption(option)
            item.setTextWidth(item.boundingRect().width())
            item.setDefaultTextColor(QColor(theme.tokens().muted))
            self._message_item = item
        self.view.resetTransform()
        self.scene.setSceneRect(self.scene.itemsBoundingRect())
        self._update_page_controls()
        self._update_search_controls()

    def render_synthesized(self, path: str) -> None:
        """Render a DOCX/PPTX/XLSX document as a plain, scrollable text
        flow -- one QGraphicsScene page per page/slide/sheet, exactly
        like `render_pdf`'s multi-page layout, but drawn with QPainter
        from the SAME Word list core.document_extractor.extract_document_words
        produces for that file, instead of rasterizing real pixels.

        This is a deliberate simplification, not an attempt at a
        faithful DOCX/PPTX/XLSX renderer: these formats either have no
        fixed pagination at all (DOCX) or no fixed point-based layout
        (XLSX), so there is no single "real" rendering to reproduce
        (see core/document_extractor.py's module docstring). What
        matters for THIS app is that the preview and the diff
        highlighter agree exactly on where every word is -- which is
        guaranteed here because both are built from the identical
        Word coordinates, not from two independent layout engines that
        could drift apart.

        Raises:
            RuntimeError: the file can't be parsed (propagated from
                core.document_extractor).
        """
        from core.document_extractor import document_kind, extract_synthesized_words, get_page_count

        try:
            words = extract_synthesized_words(path)
            # A DOCX here means no layout engine: one synthesized page.
            page_count = 1 if document_kind(path) == "docx" else get_page_count(path)
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

        words_by_page: dict[int, list] = {}
        for w in words:
            words_by_page.setdefault(w.page, []).append(w)

        self._reset_scene()

        page_width_px = int(_SYNTH_PAGE_WIDTH_PT * RENDER_DPI_SCALE)
        y = 0.0
        first_item: QGraphicsPixmapItem | None = None
        for page_number in range(1, max(page_count, 1) + 1):
            page_words = words_by_page.get(page_number, [])
            content_bottom = max((w.bottom for w in page_words), default=0.0)
            page_height_px = max(
                int((content_bottom + _SYNTH_PAGE_MARGIN_PT) * RENDER_DPI_SCALE),
                int(_SYNTH_MIN_PAGE_HEIGHT_PT * RENDER_DPI_SCALE),
            )
            pixmap = _draw_synthesized_page(page_words, page_width_px, page_height_px)
            item = self._add_page(pixmap, y)
            if page_number == 1:
                first_item = item
            self._page_y_offsets.append(y)
            y += pixmap.height() + PAGE_GAP_PX

        self._finish_pages()

        self._pixmap_item = first_item
        self._base_pixmap = None
        self._page_count = page_count
        self._current_page = 1

        self.set_zoom(1.0)
        self._update_page_controls()
        logger.info("Rendered %d synthesized page(s) of %s", page_count, path)
        kind_label = {"docx": "Word document", "pptx": "PowerPoint", "xlsx": "Excel workbook"}.get(
            document_kind(path), "Document"
        )
        unit = {"pptx": "slide", "xlsx": "sheet"}.get(document_kind(path), "page")
        self._set_file_info(
            os.path.basename(path),
            f"{kind_label} \u00b7 {page_count} {unit}{'s' if page_count != 1 else ''} \u00b7 approximate layout",
        )
        if document_kind(path) == "docx":
            self.layout_note.setText(
                "Approximate layout: install Microsoft Word or LibreOffice to see and "
                "compare this document with its real pages and tables."
            )
            self.layout_note.show()
        else:
            self.layout_note.hide()
        self._request_search_index(path, document_kind(path))
        self.file_path = path  # set before `loaded` so listeners see it
        self.loaded.emit(path)

    def render_pdf(self, path: str, source_path: str | None = None) -> None:
        """Render every page of the PDF at ``path`` into one continuous,
        vertically-stacked scene, so the whole document can be scrolled
        and paged through -- not just page 1.

        Args:
            path: Filesystem path to a PDF file.
            source_path: The document this PDF was converted from (a
                DOCX), which becomes this panel's `file_path`.

        Raises:
            RuntimeError: If the file can't be opened or has no pages.
        """
        try:
            doc = fitz.open(path)
        except Exception as exc:
            raise RuntimeError(f"'{path}' is not a readable PDF: {exc}") from exc

        if doc.page_count == 0:
            doc.close()
            raise RuntimeError("PDF has no pages.")

        sizes = [doc.load_page(i).rect for i in range(doc.page_count)]
        doc.close()
        self._reset_scene()
        self._doc_path = path

        y = 0.0
        first_item = None
        for i, rect in enumerate(sizes):
            # Same pixel size get_pixmap produces at this scale.
            pixel_rect = (rect * fitz.Matrix(RENDER_DPI_SCALE, RENDER_DPI_SCALE)).irect
            width, height = pixel_rect.width, pixel_rect.height
            frame = self._add_page_frame(width, height, y)
            if i == 0:
                first_item = frame
            self._page_y_offsets.append(y)
            y += height + PAGE_GAP_PX

        self._finish_pages()

        self._pixmap_item = first_item
        self._base_pixmap = None
        self._page_count = len(sizes)
        self._current_page = 1

        self.set_zoom(1.0)
        self._update_page_controls()
        logger.info("Rendered %d page(s) of %s", self._page_count, source_path or path)
        shown = source_path or path
        kind_label = "Word document" if source_path else "PDF"
        pages = f"{self._page_count} page{'s' if self._page_count != 1 else ''}"

        if self.source_files and len(self.source_files) > 1:
            names = [os.path.basename(f) for f in self.source_files]
            label_text = f"Merged ({len(self.source_files)} files): {' + '.join(names[:2])}"
            if len(names) > 2:
                label_text += f" + {len(names) - 2} more"
            details = f"Merged PDF \u00b7 {self._page_count} pages ({len(self.source_files)} files)"
            self._set_file_info(label_text, details)

            # Build a rich tooltip listing all constituent files with their page ranges
            tooltip_lines = [f"Merged Document ({len(self.source_files)} files, {self._page_count} total pages):"]
            for i, info in enumerate(self.source_file_infos or []):
                start = info.get("start_page", "?")
                end = info.get("end_page", "?")
                rng = f"p. {start}" if start == end else f"pp. {start}\u2013{end}"
                tooltip_lines.append(f"  {i+1}. {info.get('name', '')} ({rng}, {info.get('size_formatted', '')})")
            if not self.source_file_infos:
                for i, f in enumerate(self.source_files):
                    tooltip_lines.append(f"  {i+1}. {os.path.basename(f)}")
            self.file_label.setToolTip("\n".join(tooltip_lines))
        else:
            self._set_file_info(
                os.path.basename(shown),
                f"{kind_label} \u00b7 {pages}" + (" \u00b7 laid out by Microsoft Word" if source_path else ""),
            )
        self.layout_note.hide()
        self._request_search_index(path, "pdf")
        if self._tables_visible:
            self._request_table_layout()
        self.file_path = source_path or path  # set before `loaded` so listeners see it
        self.loaded.emit(self.file_path)

    # ------------------------------------------------------------------
    # Page navigation
    # ------------------------------------------------------------------

    def go_to_page(self, page: int) -> bool:
        """Scroll so page ``page`` (1-indexed) is at the top of the view.

        Returns:
            True if the page exists in this document and the view was
            moved; False if ``page`` is out of range (e.g. this
            document has fewer pages than the other one being compared).
        """
        if not self._page_y_offsets or page < 1 or page > len(self._page_y_offsets):
            return False
        scene_y = self._page_y_offsets[page - 1]
        # Emits view_moved via _on_scrolled: a locked partner follows.
        self.view.verticalScrollBar().setValue(round(scene_y * self._zoom_factor))
        self._current_page = page
        self._update_page_controls()
        return True

    def go_to_previous_page(self) -> None:
        self.go_to_page(self._current_page - 1)

    def go_to_next_page(self) -> None:
        self.go_to_page(self._current_page + 1)

    def _update_page_controls(self) -> None:
        has_pages = self._page_count > 0
        self.page_spin.blockSignals(True)
        self.page_spin.setRange(1, max(self._page_count, 1))
        self.page_spin.setValue(max(1, min(self._current_page, max(self._page_count, 1))))
        self.page_spin.blockSignals(False)
        self.page_spin.setEnabled(has_pages)
        self.page_label.setText(f"of {self._page_count}" if has_pages else "of \u2013")
        self.prev_page_button.setEnabled(has_pages and self._current_page > 1)
        self.next_page_button.setEnabled(has_pages and self._current_page < self._page_count)
        for button in (self.zoom_in_button, self.zoom_out_button, self.fit_button):
            button.setEnabled(has_pages)

    def _on_page_spin_changed(self, page: int) -> None:
        self.go_to_page(page)

    def _current_page_from_scroll(self) -> int:
        """Best-effort page number for the current scroll position.

        The vertical scrollbar reports viewport pixels under the active
        zoom transform, so dividing by the zoom factor gets back to
        (approximately) the unscaled scene Y used in `_page_y_offsets`.
        Exact-to-the-pixel accuracy isn't the goal here -- this is only
        used to keep the "Page X / N" label and Prev/Next buttons in
        sync while free-scrolling, not for click-to-navigate (which
        uses the precise offset directly).
        """
        if not self._page_y_offsets:
            return 1
        scene_y = self.view.verticalScrollBar().value() / max(self._zoom_factor, 1e-6)
        page_index = 0
        for i, offset in enumerate(self._page_y_offsets):
            if offset <= scene_y:
                page_index = i
            else:
                break
        return page_index + 1

    # ------------------------------------------------------------------
    # Diff navigation
    # ------------------------------------------------------------------

    def highlight_pdf_bbox(
        self, page: int, boxes: list[tuple[float, float, float, float]]
    ) -> bool:
        """Highlight one or more regions given in raw PDF-point coordinates
        and scroll to them.

        Each entry in ``boxes`` is a (x0, top, x1, bottom) rectangle for
        ONE visual text line (as produced by `core/diff_engine.py`'s
        `Location.boxes`). A discrepancy spanning many lines therefore
        draws a separate horizontal highlight per line -- not one tall
        vertical rectangle merging top-to-bottom text and slicing
        through unrelated content.

        Single-page convenience wrapper around `highlight_location` for
        callers that already have a bare (page, boxes) pair rather than
        a full Location (e.g. a "missing" marker mirroring the OTHER
        document's coordinates, where there is no Location of this
        document's own). Prefer `highlight_location` for anything that
        might span more than one page.

        Args:
            page: 1-indexed page the boxes belong to.
            boxes: Per-line rectangles in PDF points -- NOT scene/pixel
                coordinates. At minimum one box (the union `Location.bbox`
                works as a fallback for navigation).

        Returns:
            True if the highlight was drawn. False only if ``page``
            doesn't exist in THIS document -- e.g. the two PDFs being
            compared have a different page count and this particular
            page only exists on the other side.
        """
        self._clear_highlight_items()
        drawn = self._draw_highlight_rects(page, boxes)
        if not drawn:
            return False
        self._navigate_to(self._union_rect(drawn))
        self._current_page = page
        self._update_page_controls()
        return True

    def highlight_location(self, location, *, style: str = "diff", navigate: bool = True) -> bool:
        """Highlight EVERY page a `core.diff_engine.Location` touches,
        not just its primary page -- the fix for a discrepancy whose
        text spans a page boundary, or (the case this exists for) a
        discrepancy being looked up against a document with a
        DIFFERENT page count than the one the Location's page numbers
        came from (e.g. a multi-page document vs. a 1-page "concise"
        counterpart): iterating every (page, boxes) pair means a page
        that exists in THIS document still gets its highlight drawn
        even if another page the Location also touches doesn't exist
        here.

        Scrolls/centers on the FIRST page that has a rect in THIS
        document (not necessarily the Location's own primary page --
        this document may not have that page at all).

        ``style="sync"`` draws the blue "same text" marker used when a
        click in one viewer is mirrored to the other. ``navigate=False``
        draws without moving the view.

        Returns:
            True if at least one page's highlight was drawn.
        """
        self._clear_highlight_items()
        all_rects: list[QRectF] = []
        first_page_rects: list[QRectF] | None = None
        first_page: int | None = None
        for page, boxes in location.iter_pages():
            drawn = self._draw_highlight_rects(page, boxes, style)
            if not drawn:
                continue
            all_rects.extend(drawn)
            if first_page_rects is None:
                first_page_rects = drawn
                first_page = page
        if not all_rects:
            logger.info(
                "Location spans page(s) %s but this document has %d page(s) -- "
                "none matched.",
                [p for p, _ in location.iter_pages()],
                self._page_count,
            )
            return False
        if navigate:
            self._navigate_to(self._union_rect(first_page_rects))
            self._current_page = first_page
            self._update_page_controls()
        return True

    def _clear_highlight_items(self) -> None:
        for item in self._highlight_items:
            try:
                self.scene.removeItem(item)
            except RuntimeError:
                pass
        self._highlight_items = []

    def _draw_highlight_rects(
        self, page: int, boxes: list[tuple[float, float, float, float]], style: str = "diff"
    ) -> list[QRectF]:
        """Draw (without clearing or navigating) the highlight rects for
        one page. Returns the rects actually drawn -- empty if ``page``
        doesn't exist in this document."""
        rects = self._scene_rects_for(page, boxes)
        if not rects:
            if rects is None:
                logger.info(
                    "Diff is on page %s but this document only has %d page(s).",
                    page,
                    self._page_count,
                )
            return []
        pen, brush = (_SYNC_PEN, _SYNC_BRUSH) if style == "sync" else (_HIGHLIGHT_PEN, _HIGHLIGHT_BRUSH)
        for rect in rects:
            item = self.scene.addRect(rect, pen, brush)
            item.setZValue(2)  # above the severity overlays
            self._highlight_items.append(item)
        return rects

    def highlight_missing_bbox(
        self, page: int, boxes: list[tuple[float, float, float, float]]
    ) -> bool:
        """Mark where text is MISSING on this document and scroll to it.

        For an insertion/deletion the text only exists on ONE side; the
        other side has no location of its own. This method draws a
        dashed red marker at the SAME (page, boxes) the text occupies on
        the other document -- the "before coordinates" -- and scrolls
        THIS panel to the marker, so a removed/added item navigates
        BOTH documents to the same spot, not just the side that still
        has the text.

        Args:
            page, boxes: Coordinates from the OTHER document (raw PDF
                points, as in `highlight_pdf_bbox`). Each box is one
                visual text line, drawn as its own dashed rect.

        Returns:
            True if the marker was drawn (and the panel scrolled).
            False only if this document has no pages at all -- when
            ``page`` exceeds this document's page count, the marker is
            clamped to a band across the bottom of its LAST page so
            the panel still navigates somewhere meaningful instead of
            silently doing nothing.
        """
        for item in self._missing_items:
            try:
                self.scene.removeItem(item)
            except RuntimeError:
                pass
        self._missing_items = []

        rects = self._scene_rects_for(page, boxes)
        if rects is None:
            # ``page`` doesn't exist in THIS document (e.g. the other
            # PDF has more pages). The missing content is absent from
            # the whole region AFTER the last page, so draw the marker
            # as a full-width band at the bottom of the last page and
            # navigate there -- the reviewer still sees where this
            # document's content ends.
            if self._page_count == 0:
                return False
            band_height = 48.0
            last_page = self._page_rects[-1]
            rect = QRectF(
                last_page.left(),
                last_page.bottom() - band_height,
                last_page.width(),
                band_height,
            )
            rects = [rect]
            page = self._page_count

        for rect in rects:
            self._missing_items.append(
                self.scene.addRect(rect, _MISSING_PEN, _MISSING_BRUSH)
            )
        # Navigate this panel itself rather than relying on the mirrored
        # scrollbar: centerOn places THIS document's (page, bbox) in the
        # viewport using its own page offsets, and it also moves the
        # horizontal scrollbar, which the mirror never does.
        self._navigate_to(self._union_rect(rects))
        self._current_page = page
        self._update_page_controls()
        return True

    def highlight_missing_location(self, location) -> bool:
        """Multi-page-aware version of `highlight_missing_bbox`: mirrors
        EVERY page a `core.diff_engine.Location` from the OTHER
        document touches onto this one, drawing a dashed "missing"
        marker on whichever of those pages exist here, and clamping to
        this document's last page only if NONE of them do (the same
        page-count-mismatch case `highlight_missing_bbox` already
        handles for a single page).
        """
        for item in self._missing_items:
            try:
                self.scene.removeItem(item)
            except RuntimeError:
                pass
        self._missing_items = []

        all_rects: list[QRectF] = []
        first_page_rects: list[QRectF] | None = None
        first_page: int | None = None
        for page, boxes in location.iter_pages():
            rects = self._scene_rects_for(page, boxes)
            if not rects:
                continue
            for rect in rects:
                self._missing_items.append(
                    self.scene.addRect(rect, _MISSING_PEN, _MISSING_BRUSH)
                )
            all_rects.extend(rects)
            if first_page_rects is None:
                first_page_rects = rects
                first_page = page

        if not all_rects:
            # None of the Location's pages exist in THIS document (a
            # shorter document being compared against a longer one).
            # Clamp to a band at the bottom of this document's last
            # page, same fallback as highlight_missing_bbox, so the
            # panel still navigates somewhere meaningful.
            if self._page_count == 0:
                return False
            band_height = 48.0
            last_page = self._page_rects[-1]
            rect = QRectF(last_page.left(), last_page.bottom() - band_height, last_page.width(), band_height)
            self._missing_items.append(self.scene.addRect(rect, _MISSING_PEN, _MISSING_BRUSH))
            self._navigate_to(rect)
            self._current_page = self._page_count
            self._update_page_controls()
            return True

        self._navigate_to(self._union_rect(first_page_rects))
        self._current_page = first_page
        self._update_page_controls()
        return True

    @staticmethod
    def _union_rect(rects: list[QRectF]) -> QRectF:
        """Union of ``rects`` -- the navigation target for a multi-line
        highlight (each line is drawn separately, but the view centers
        on the whole group)."""
        union = QRectF(rects[0])
        for rect in rects[1:]:
            union = union.united(rect)
        return union

    def _navigate_to(self, rect: QRectF) -> None:
        """Center the view on ``rect`` WITHOUT feeding the scroll mirror.

        When main.py activates a diff result it navigates BOTH panels to
        their own markers. If either navigation emitted ``view_moved``,
        a locked partner would be moved off its own highlight/marker.
        Suppressing the signal here lets each panel land on its own
        marker; locking stays in effect for ordinary user scrolling.
        """
        self._syncing_scroll = True
        try:
            self.view.centerOn(rect.center())
        finally:
            self._syncing_scroll = False

    def _scene_rects_for(
        self, page: int, boxes: list[tuple[float, float, float, float]]
    ) -> list[QRectF] | None:
        """Scene-coordinate rects for PDF-point (page, boxes), or None if
        this document doesn't have ``page``.

        Shared by `highlight_pdf_bbox` and `highlight_missing_bbox` so
        the "found" and "missing" markers always land on the same spots.
        """
        if not self._page_y_offsets or page < 1 or page > len(self._page_y_offsets):
            return None
        page_y_offset = self._page_y_offsets[page - 1]
        rects: list[QRectF] = []
        for bbox in boxes:
            x0, top, x1, bottom = bbox
            rect = QRectF(
                x0 * RENDER_DPI_SCALE,
                page_y_offset + top * RENDER_DPI_SCALE,
                (x1 - x0) * RENDER_DPI_SCALE,
                (bottom - top) * RENDER_DPI_SCALE,
            )
            # A little padding so the highlight isn't drawn flush against
            # the glyphs -- purely cosmetic.
            rect.adjust(-2, -2, 2, 2)
            rects.append(rect)
        return rects

    def clear_highlight(self) -> None:
        """Remove any current highlight or missing-marker (e.g. before a
        fresh comparison run or before drawing a new result's markers)."""
        for item in self._highlight_items + self._missing_items:
            try:
                self.scene.removeItem(item)
            except RuntimeError:
                pass
        self._highlight_items = []
        self._missing_items = []

    # ------------------------------------------------------------------
    # Persistent severity-colored overlays (all discrepancies at once)
    # ------------------------------------------------------------------

    def draw_all_overlays(self, diff_items, side: str) -> None:
        """Draw translucent, severity-colored overlays for EVERY
        discrepancy onto the scene, keeping them as a separate layer from
        the click-to-navigate highlights.

        Args:
            diff_items: The full list of DiffItem results.
            side: "approved" or "suspect" -- which Location to pick from
                each DiffItem.
        """
        self.clear_overlays()
        for item in diff_items:
            loc = item.approved_location if side == "approved" else item.suspect_location
            # For a pure insertion/deletion the text doesn't exist on
            # this side -- use the caret marker instead (a small rect at
            # the insertion point).
            if loc is None:
                loc = item.approved_caret if side == "approved" else item.suspect_caret
            if loc is None:
                continue

            sev = item.severity
            brush = QBrush(_SEVERITY_OVERLAY_COLORS.get(sev, _DEFAULT_OVERLAY_COLOR))
            pen = _SEVERITY_OVERLAY_PENS.get(sev, _DEFAULT_OVERLAY_PEN)

            for page, boxes in loc.iter_pages():
                rects = self._scene_rects_for(page, boxes)
                if not rects:
                    continue
                for rect in rects:
                    overlay = self.scene.addRect(rect, pen, brush)
                    overlay.setZValue(1)  # above the pixmap, below nav highlight
                    self._overlay_items.append(overlay)
                    self._overlay_hits.append((rect, item))

    def draw_matched(self, boxes_by_page: dict) -> None:
        """Tint in very light green the text the comparison found the same
        in both documents: {page: [box in PDF points]} (see
        `core.token_map.TokenMap.matched_boxes`). Drawn under the
        difference highlights."""
        self.clear_matched()
        for page, boxes in boxes_by_page.items():
            if page < 1 or page > len(self._page_y_offsets):
                continue
            offset = self._page_y_offsets[page - 1]
            for x0, top, x1, bottom in boxes:
                rect = QRectF(
                    x0 * RENDER_DPI_SCALE - 1, offset + top * RENDER_DPI_SCALE - 1,
                    (x1 - x0) * RENDER_DPI_SCALE + 2, (bottom - top) * RENDER_DPI_SCALE + 2,
                )
                item = self.scene.addRect(rect, _MATCHED_PEN, _MATCHED_BRUSH)
                item.setZValue(0.5)  # above the page image, under every other marker
                self._matched_items.append(item)

    def clear_matched(self) -> None:
        for item in self._matched_items:
            try:
                self.scene.removeItem(item)
            except RuntimeError:
                pass
        self._matched_items = []

    def matched_count(self) -> int:
        return len(self._matched_items)

    def clear_overlays(self) -> None:
        """Remove all persistent severity overlays."""
        for item in self._overlay_items:
            try:
                self.scene.removeItem(item)
            except RuntimeError:
                pass
        self._overlay_items = []
        self._overlay_hits = []

    def item_at(self, page: int, x: float, y: float):
        """The difference whose overlay contains the PDF point, or None.
        When overlays overlap, the smallest (most specific) one wins."""
        rects = self._scene_rects_for(page, [(x, y, x, y)])
        if not rects:
            return None
        point = rects[0].center()
        hits = [(r.width() * r.height(), item) for r, item in self._overlay_hits if r.contains(point)]
        return min(hits, key=lambda hit: hit[0])[1] if hits else None

    def set_overlays_visible(self, visible: bool) -> None:
        """Toggle visibility of the persistent overlays (for blinking)."""
        for item in self._overlay_items:
            item.setVisible(visible)

    # ------------------------------------------------------------------
    # Crop region overlays
    # ------------------------------------------------------------------

    def draw_crop_overlays(
        self, page_crops: dict[int, tuple]
    ) -> None:
        """Draw semi-transparent red crop bands on the rendered pages.

        Args:
            page_crops: ``{page_number: (header_bottom_y_pt, footer_top_y_pt, [left_x_pt, right_x_pt])}``
                in PDF points. Pages not in the dict have no crop.
        """
        self.clear_crop_overlays()
        if not page_crops or not self._page_y_offsets:
            return

        crop_brush = QBrush(QColor(220, 40, 40, 60))
        crop_pen = QPen(QColor(220, 40, 40, 140), 1.5, Qt.PenStyle.DashLine)

        for page, crop in page_crops.items():
            if page < 1 or page > len(self._page_y_offsets):
                continue
            header_bottom_pt = float(crop[0])
            footer_top_pt = float(crop[1])
            left_pt = float(crop[2]) if len(crop) >= 4 else 0.0

            page_offset = self._page_y_offsets[page - 1]
            page_h_pt = 800.0
            page_w_pt = 600.0
            scene_w = self.scene.sceneRect().width()
            if page <= len(self._page_rects):
                page_h_pt = self._page_rects[page - 1].height() / RENDER_DPI_SCALE
                page_w_pt = self._page_rects[page - 1].width() / RENDER_DPI_SCALE
                scene_w = self._page_rects[page - 1].width()

            page_h_px = page_h_pt * RENDER_DPI_SCALE
            right_pt = float(crop[3]) if len(crop) >= 4 else page_w_pt

            # Header crop band
            if header_bottom_pt > 0.0:
                h_scene = header_bottom_pt * RENDER_DPI_SCALE
                rect = QRectF(0, page_offset, scene_w, h_scene)
                item = self.scene.addRect(rect, crop_pen, crop_brush)
                item.setZValue(2)  # above pixmap, below severity overlays
                self._crop_overlay_items.append(item)

            # Footer crop band
            if footer_top_pt < page_h_pt:
                f_y = page_offset + footer_top_pt * RENDER_DPI_SCALE
                f_h = (page_h_pt - footer_top_pt) * RENDER_DPI_SCALE
                rect = QRectF(0, f_y, scene_w, f_h)
                item = self.scene.addRect(rect, crop_pen, crop_brush)
                item.setZValue(2)
                self._crop_overlay_items.append(item)

            # Left crop band
            if left_pt > 0.0:
                l_w = left_pt * RENDER_DPI_SCALE
                rect = QRectF(0, page_offset, l_w, page_h_px)
                item = self.scene.addRect(rect, crop_pen, crop_brush)
                item.setZValue(2)
                self._crop_overlay_items.append(item)

            # Right crop band
            if right_pt < page_w_pt:
                r_x = right_pt * RENDER_DPI_SCALE
                r_w = (page_w_pt - right_pt) * RENDER_DPI_SCALE
                rect = QRectF(r_x, page_offset, r_w, page_h_px)
                item = self.scene.addRect(rect, crop_pen, crop_brush)
                item.setZValue(2)
                self._crop_overlay_items.append(item)


    def clear_crop_overlays(self) -> None:
        """Remove all crop region overlays."""
        for item in self._crop_overlay_items:
            try:
                self.scene.removeItem(item)
            except RuntimeError:
                pass
        self._crop_overlay_items = []

    # ------------------------------------------------------------------
    # Zoom
    # ------------------------------------------------------------------

    def _on_view_zoom_requested(self, relative: float, anchor: QPointF) -> None:
        self.zoom_by(relative, anchor)

    def zoom_by(self, relative: float, anchor: QPointF | None = None) -> None:
        """User zoom on THIS panel only, keeping ``anchor`` (a scene
        point; default: the view center) where it is on screen."""
        if self._pixmap_item is None:
            return
        self.set_zoom(self._zoom_factor * relative, anchor)
        self.interacted.emit()
        self.zoom_changed.emit(self._zoom_factor)

    def _on_fit_clicked(self) -> None:
        if self._pixmap_item is None:
            return
        self.fit_to_width()
        self.interacted.emit()
        self.zoom_changed.emit(self._zoom_factor)

    def set_zoom(self, factor: float, anchor: QPointF | None = None) -> None:
        """Set the absolute zoom level (1.0 = native render resolution)
        without emitting ``zoom_changed``.

        Args:
            factor: Zoom multiplier, clamped to [0.1, 8.0] per the spec's
                10%-800% requirement.
            anchor: Scene point that stays at the same place in the
                viewport (default: the view center), so zooming never
                makes the view jump to a different part of the document.
        """
        if self._pixmap_item is None:
            return
        factor = max(MIN_ZOOM, min(MAX_ZOOM, factor))
        viewport = self.view.viewport().rect()
        if anchor is None:
            anchor = self.view.mapToScene(viewport.center())
        anchor_on_screen = self.view.mapFromScene(anchor)
        self._syncing_scroll = True
        try:
            self._zoom_factor = factor
            self.view.resetTransform()
            self.view.scale(factor, factor)
            # Put the anchor back under the same viewport pixel.
            moved = self.view.mapFromScene(anchor) - anchor_on_screen
            hbar = self.view.horizontalScrollBar()
            vbar = self.view.verticalScrollBar()
            hbar.setValue(hbar.value() + moved.x())
            vbar.setValue(vbar.value() + moved.y())
        finally:
            self._syncing_scroll = False
        self.zoom_label.setText(f"{round(factor * 100)}%")
        self._render_timer.start()
        self._current_page = self._current_page_from_scroll()
        self._update_page_controls()

    def fit_to_width(self) -> None:
        """Scale so the widest page fills the viewport width."""
        if self._pixmap_item is None:
            return
        page_width = max((rect.width() for rect in self._page_rects), default=0) + 2 * _CANVAS_MARGIN
        if page_width <= 0:
            return
        viewport_width = self.view.viewport().width() - 4  # avoid a horizontal scrollbar
        self.set_zoom(viewport_width / page_width)

    def zoom_factor(self) -> float:
        """Return the current zoom multiplier."""
        return self._zoom_factor

    # ------------------------------------------------------------------
    # Positions in PDF points (for keeping two panels in step)
    # ------------------------------------------------------------------

    def pdf_point(self, scene_pos: QPointF) -> tuple[int, float, float] | None:
        """(page, x, y) in PDF points for a scene position, or None
        outside every page."""
        if not self._page_y_offsets:
            return None
        page_index = 0
        for i, offset in enumerate(self._page_y_offsets):
            if offset <= scene_pos.y():
                page_index = i
            else:
                break
        return (
            page_index + 1,
            scene_pos.x() / RENDER_DPI_SCALE,
            (scene_pos.y() - self._page_y_offsets[page_index]) / RENDER_DPI_SCALE,
        )

    def view_position(self) -> tuple[int, float, float] | None:
        """(page, x, y) in PDF points at the center of the view."""
        return self.pdf_point(self.view.mapToScene(self.view.viewport().rect().center()))

    def center_on_position(self, page: int, x: float, y: float) -> None:
        """Center the view on (page, x, y) in PDF points, clamping the
        page to this document's range. Does not emit ``view_moved``."""
        if not self._page_y_offsets:
            return
        page = max(1, min(page, len(self._page_y_offsets)))
        center = QPointF(
            x * RENDER_DPI_SCALE, self._page_y_offsets[page - 1] + y * RENDER_DPI_SCALE
        )
        self._navigate_to(QRectF(center, center))
        self._current_page = self._current_page_from_scroll()
        self._update_page_controls()

    def _on_view_clicked(self, scene_pos: QPointF) -> None:
        point = self.pdf_point(scene_pos)
        if point is not None:
            self.point_clicked.emit(*point)

    def _on_view_double_clicked(self, scene_pos: QPointF) -> None:
        point = self.pdf_point(scene_pos)
        if point is not None:
            self.point_double_clicked.emit(*point)

    # ------------------------------------------------------------------
    # Area selection (a manual rematch: core/rematch.rematch_areas)
    # ------------------------------------------------------------------

    def set_area_mode(self, on: bool) -> None:
        """While on, a plain drag draws an area instead of panning."""
        self.view.area_mode = on
        self.view.setDragMode(
            QGraphicsView.DragMode.NoDrag if on else QGraphicsView.DragMode.ScrollHandDrag
        )
        if on:
            self.view.viewport().setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.view.viewport().unsetCursor()

    def area_selection(self) -> list[tuple[int, tuple[float, float, float, float]]]:
        """The area drawn on this document, per page, in PDF points."""
        return list(self._area)

    def clear_area_selection(self) -> None:
        for item in self._area_items:
            try:
                self.scene.removeItem(item)
            except RuntimeError:
                pass
        self._area_items = []
        if self._area:
            self._area = []
            self.area_selected.emit()

    def select_area(self, page: int, box: tuple[float, float, float, float], add: bool = False) -> None:
        """Select ``box`` (PDF points) on ``page``, as a drag would."""
        rects = self._scene_rects_for(page, [box])
        if rects:
            rect = rects[0].adjusted(2, 2, -2, -2)  # undo the highlight padding
            self._on_area_dragged(rect, add)

    def _on_area_dragged(self, rect: QRectF, add: bool) -> None:
        boxes = self._pdf_boxes_in(rect)
        if not add:
            for item in self._area_items:
                try:
                    self.scene.removeItem(item)
                except RuntimeError:
                    pass
            self._area_items = []
            self._area = []
        if boxes:
            accent = QColor(theme.tokens().accent)
            pen = QPen(accent, 2, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            fill = QColor(accent)
            fill.setAlpha(28)
            for page, box in boxes:
                for scene_rect in self._scene_rects_for(page, [box]) or []:
                    item = self.scene.addRect(scene_rect.adjusted(2, 2, -2, -2), pen, QBrush(fill))
                    item.setZValue(4)  # above the difference overlays
                    self._area_items.append(item)
            self._area.extend(boxes)
        self.area_selected.emit()

    def _pdf_boxes_in(self, rect: QRectF) -> list[tuple[int, tuple[float, float, float, float]]]:
        """A scene rectangle as (page, box in PDF points), one per page
        it crosses -- a drag can run from the foot of one page onto the
        next."""
        boxes = []
        for index, page_rect in enumerate(self._page_rects):
            part = rect.intersected(page_rect)
            if part.isEmpty():
                continue
            top = self._page_y_offsets[index]
            boxes.append((
                index + 1,
                (
                    part.left() / RENDER_DPI_SCALE,
                    (part.top() - top) / RENDER_DPI_SCALE,
                    part.right() / RENDER_DPI_SCALE,
                    (part.bottom() - top) / RENDER_DPI_SCALE,
                ),
            ))
        return boxes

    # ------------------------------------------------------------------
    # Table cells overlay (View > Show Table Cells)
    # ------------------------------------------------------------------

    def set_table_cells_visible(self, visible: bool) -> None:
        """Outline every table and cell the comparison detects on this
        document's pages (read in the background the first time)."""
        self._tables_visible = visible
        self._clear_table_items()
        if not visible:
            return
        if self._table_layout_for != self._layout_key():
            self._request_table_layout()
        for page, tables in sorted(self._table_layout.items()):
            self._draw_page_tables(page, tables)

    def table_cells_available(self) -> bool:
        """Whether this document has real pages to analyze (a PDF, or a
        Word document laid out by Word) -- not an approximate layout."""
        return self._doc_path is not None

    def detected_tables(self) -> dict[int, list[dict]]:
        """The table layout read so far: {page: [{"bbox", "cells",
        "status", "parent"}]} in PDF points."""
        return dict(self._table_layout)

    def set_crop(self, crop: dict | None) -> None:
        """The margins cut off this document ({page: (header_bottom,
        footer_top, left, right)}): the table outlines are worked out
        from the kept part, as the comparison reads it."""
        crop = dict(crop or {})
        if crop == self._crop:
            return
        self._crop = crop
        if self._tables_visible:
            self.set_table_cells_visible(True)

    def _layout_key(self) -> tuple:
        return (self._generation, tuple(sorted((p, tuple(m)) for p, m in self._crop.items())))

    def _request_table_layout(self) -> None:
        if self._doc_path is None:
            return
        self._table_layout = {}
        self._table_layout_for = self._layout_key()
        self._layout_request += 1
        self._layout_reader.request(self._layout_request, self._doc_path, dict(self._crop))

    def _on_table_layout(self, request: int, layout: dict) -> None:
        if request != self._layout_request:
            return  # for a document (or crop) no longer shown
        self._table_layout.update(layout)
        if self._tables_visible:
            for page, tables in sorted(layout.items()):
                self._draw_page_tables(page, tables)

    def _clear_table_items(self) -> None:
        for item in self._table_items:
            try:
                self.scene.removeItem(item)
            except RuntimeError:
                pass
        self._table_items = []

    def _exact_scene_rect(self, page: int, box) -> QRectF | None:
        if page < 1 or page > len(self._page_y_offsets):
            return None
        x0, top, x1, bottom = box
        offset = self._page_y_offsets[page - 1]
        return QRectF(
            x0 * RENDER_DPI_SCALE, offset + top * RENDER_DPI_SCALE,
            (x1 - x0) * RENDER_DPI_SCALE, (bottom - top) * RENDER_DPI_SCALE,
        )

    def _draw_page_tables(self, page: int, tables: list[dict]) -> None:
        for number, table in enumerate(tables, start=1):
            outline = self._exact_scene_rect(page, table["bbox"])
            if outline is None:
                continue
            status = table.get("status", "table")
            color = QColor(_TABLE_COLORS.get(status, _TABLE_COLORS["table"]))
            dashed = status == "ignored"
            cell_color = QColor(color)
            cell_color.setAlpha(150)
            cell_pen = QPen(cell_color, 1, Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine)
            cell_pen.setCosmetic(True)
            for row in table["cells"]:
                for cell in row:
                    rect = self._exact_scene_rect(page, cell) if cell is not None else None
                    if rect is not None:
                        item = self.scene.addRect(rect.adjusted(1.5, 1.5, -1.5, -1.5), cell_pen)
                        item.setZValue(1.6)
                        self._table_items.append(item)
            pen = QPen(color, 2.2, Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine)
            pen.setCosmetic(True)
            frame = self.scene.addRect(outline, pen)
            frame.setZValue(1.7)
            self._table_items.append(frame)
            rows = len(table["cells"])
            cols = max((len(row) for row in table["cells"]), default=0)
            self._table_items.extend(
                self._table_tag(f"{_TABLE_LABELS.get(status, 'Table')} {number} \u00b7 {rows}\u00d7{cols}", color, outline)
            )

    def _table_tag(self, text: str, color: QColor, outline: QRectF) -> list:
        """A small label at a table's top-left corner, the same size at
        any zoom."""
        font = QFont()
        font.setPointSizeF(7.5)
        font.setBold(True)
        label = self.scene.addSimpleText(text, font)
        label.setBrush(QBrush(QColor(255, 255, 255)))
        label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        bounds = label.boundingRect().adjusted(-4, -2, 4, 2)
        back = self.scene.addRect(bounds, QPen(Qt.PenStyle.NoPen), QBrush(color))
        back.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        back.setPos(outline.left() + 0.0, outline.top())
        label.setPos(outline.left() + 0.0, outline.top())
        back.setZValue(1.8)
        label.setZValue(1.9)
        # Above the frame, not over the table's first cell.
        back.setTransform(back.transform().translate(4, -bounds.height() - 1))
        label.setTransform(label.transform().translate(4, -bounds.height() - 1))
        return [back, label]

    # ------------------------------------------------------------------
    # Find bar (core/text_search.py)
    # ------------------------------------------------------------------

    def focus_search(self) -> None:
        """Put the cursor in this document's Find bar, query selected."""
        self.search_field.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_field.selectAll()

    def search(self, query: str) -> int:
        """Search for ``query`` now and go to the first match at or after
        the view; returns how many matches there are (-1 while the
        document's text is still being read)."""
        self.search_field.blockSignals(True)
        self.search_field.setText(query)
        self.search_field.blockSignals(False)
        self._search_timer.stop()
        self._run_search(navigate=True)
        return len(self._search_matches) if self._search_index is not None else -1

    def search_matches(self) -> list:
        """The matches for the current query (core.text_search.SearchMatch)."""
        return list(self._search_matches)

    def current_search_match(self) -> int:
        """Index of the match being shown, or -1."""
        return self._search_current

    def find_next(self) -> None:
        self._step_search(1)

    def find_previous(self) -> None:
        self._step_search(-1)

    def _request_search_index(self, path: str, kind: str) -> None:
        self._indexing = True
        self._indexer.request(self._generation, path, kind)
        self._update_search_controls()

    def _on_indexed(self, generation: int, index) -> None:
        if generation != self._generation:
            return  # a document that's no longer shown
        self._search_index = index
        self._indexing = False
        navigate, self._search_navigate_when_ready = self._search_navigate_when_ready, False
        self._run_search(navigate=navigate)

    def _on_search_text_changed(self, _text: str) -> None:
        self.interacted.emit()
        self._search_timer.start()

    def _on_search_escaped(self) -> None:
        if self.search_field.text():
            self.search_field.clear()
            self._search_timer.stop()
            self._run_search(navigate=False)
        else:
            self.view.setFocus()

    def _clear_search_items(self, items: list[QGraphicsRectItem]) -> None:
        for item in items:
            try:
                self.scene.removeItem(item)
            except RuntimeError:
                pass
        items.clear()

    def _run_search(self, *, navigate: bool) -> None:
        """Find the query again, tint every match, and (``navigate``) go
        to the first one at or after what's on screen."""
        self._clear_search_items(self._search_items)
        self._clear_search_items(self._search_current_items)
        self._search_matches = []
        self._search_current = -1
        query = self.search_field.text().strip()
        if query and self._search_index is None:
            # Still reading the document: search as soon as it's read.
            self._search_navigate_when_ready = self._search_navigate_when_ready or navigate
        elif query:
            self._search_matches = self._search_index.find_all(
                query,
                match_case=self.match_case_button.isChecked(),
                whole_word=self.whole_word_button.isChecked(),
            )
            for match in self._search_matches[:_MAX_TINTED_MATCHES]:
                for page, boxes in match.iter_pages():
                    for rect in self._scene_rects_for(page, boxes) or []:
                        item = self.scene.addRect(rect, _SEARCH_PEN, _SEARCH_BRUSH)
                        item.setZValue(3)  # above the difference overlays
                        self._search_items.append(item)
            if navigate and self._search_matches:
                self._search_current = self._match_from_view(1)
                self._show_current_match()
        self._update_search_controls()

    def _match_from_view(self, step: int) -> int:
        """The first match at/after the top of the view (``step`` 1), or
        the last one before it (-1), wrapping around the document."""
        top = self.pdf_point(self.view.mapToScene(self.view.viewport().rect().topLeft()))
        here = (top[0], top[2]) if top else (1, 0.0)
        keys = [(m.page, m.pages[0][1][0][1]) for m in self._search_matches]
        if step > 0:
            return next((i for i, key in enumerate(keys) if key >= here), 0)
        return next((i for i in range(len(keys) - 1, -1, -1) if keys[i] < here), len(keys) - 1)

    def _step_search(self, step: int) -> None:
        if self.search_field.text().strip() and self._search_timer.isActive():
            self._search_timer.stop()  # typed but not yet searched
            self._run_search(navigate=True)
            return
        if not self._search_matches:
            return
        if self._search_current < 0:
            self._search_current = self._match_from_view(step)
        else:
            self._search_current = (self._search_current + step) % len(self._search_matches)
        self._show_current_match()
        self._update_search_controls()

    def _show_current_match(self) -> None:
        """Outline the current match and bring it on screen. The view
        moves as a scroll would, so a locked partner follows."""
        self._clear_search_items(self._search_current_items)
        match = self._search_matches[self._search_current]
        first: list[QRectF] = []
        for page, boxes in match.iter_pages():
            rects = self._scene_rects_for(page, boxes) or []
            for rect in rects:
                item = self.scene.addRect(rect, _SEARCH_CURRENT_PEN, _SEARCH_CURRENT_BRUSH)
                item.setZValue(3.5)
                self._search_current_items.append(item)
            if rects and not first:
                first = rects
        if not first:
            return
        target = self._union_rect(first)
        visible = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        if not visible.contains(target):
            self.view.centerOn(target.center())
        self._current_page = match.page
        self._update_page_controls()

    def _update_search_controls(self) -> None:
        query = self.search_field.text().strip()
        count = len(self._search_matches)
        if not query:
            text = ""
        elif self._search_index is None:
            text = "Reading\u2026" if self._indexing else ""
        elif not count:
            text = "No matches"
        elif self._search_current < 0:
            text = f"{count} match{'es' if count != 1 else ''}"
        else:
            text = f"{self._search_current + 1} of {count}"
        self.search_count_label.setText(text)
        self.search_prev_button.setEnabled(count > 0)
        self.search_next_button.setEnabled(count > 0)

    # ------------------------------------------------------------------
    # Scroll sync plumbing
    # ------------------------------------------------------------------

    def _on_scrolled(self, _value: int) -> None:
        self._render_timer.start()
        if not self._syncing_scroll:
            self.view_moved.emit()
        self._current_page = self._current_page_from_scroll()
        self._update_page_controls()
