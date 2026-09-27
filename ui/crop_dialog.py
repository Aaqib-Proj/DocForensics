"""Crop Header/Footer dialog (ui/crop_dialog.py).

A fast, responsive modal dialog for visually defining header and footer
exclusion zones on Original and Suspect documents.

Key features:
  - Instant open (lazy on-demand page rendering, no upfront freeze)
  - Simultaneous Header and Footer exclusion zones
  - Interactive dotted lines with draggable handles (SizeVerCursor)
  - Viewport-scaled proximity detection for effortless grabbing at any zoom
  - Numeric spinbox controls with point-level precision
  - Quick "Apply to All Pages" / "Apply to This Page"
  - Document switcher (Original vs Suspect) with PDF & Word (.docx) support
  - Fast page navigation and zooming
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import fitz  # PyMuPDF
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

# Must match document_panel.py's RENDER_DPI_SCALE so coordinates align
RENDER_DPI_SCALE = 200 / 72

# Crop styling tokens
_HEADER_PEN = QPen(QColor(244, 67, 54, 240), 2, Qt.PenStyle.DashLine)
_HEADER_PEN.setCosmetic(True)

_FOOTER_PEN = QPen(QColor(244, 67, 54, 240), 2, Qt.PenStyle.DashLine)
_FOOTER_PEN.setCosmetic(True)

_CROP_OVERLAY_BRUSH = QBrush(QColor(220, 40, 40, 65))
_HANDLE_BG_BRUSH = QBrush(QColor(198, 40, 40, 230))
_HANDLE_BORDER_PEN = QPen(QColor(255, 255, 255, 210), 1)
_HANDLE_BORDER_PEN.setCosmetic(True)

_DIALOG_STYLE = """
    QDialog {
        background-color: #1e1e1e;
        color: #e0e0e0;
    }
    QLabel {
        color: #e0e0e0;
        font-size: 13px;
    }
    QPushButton {
        background-color: #2d2d30;
        color: #f0f0f0;
        border: 1px solid #3e3e42;
        border-radius: 5px;
        padding: 5px 12px;
        font-size: 12px;
    }
    QPushButton:hover {
        background-color: #3e3e42;
        border-color: #007acc;
    }
    QPushButton:pressed {
        background-color: #007acc;
        color: white;
    }
    QPushButton:disabled {
        background-color: #252526;
        color: #656565;
        border-color: #2d2d30;
    }
    QDoubleSpinBox {
        background-color: #252526;
        color: #ffffff;
        border: 1px solid #3e3e42;
        border-radius: 4px;
        padding: 3px 6px;
        font-size: 12px;
        font-weight: bold;
    }
    QDoubleSpinBox:focus {
        border-color: #007acc;
    }
    QGraphicsView {
        border: 1px solid #3e3e42;
        border-radius: 4px;
        background-color: #141414;
    }
"""

_ACTIVE_DOC_BTN = """
    QPushButton {
        background-color: #007acc;
        color: white;
        border: 2px solid #0098ff;
        border-radius: 6px;
        padding: 7px 18px;
        font-weight: bold;
        font-size: 13px;
    }
"""

_INACTIVE_DOC_BTN = """
    QPushButton {
        background-color: #2d2d30;
        color: #aaaaaa;
        border: 1px solid #3e3e42;
        border-radius: 6px;
        padding: 7px 18px;
        font-size: 13px;
    }
    QPushButton:hover {
        background-color: #3e3e42;
        color: white;
    }
"""

_APPLY_ALL_BTN_STYLE = """
    QPushButton {
        background-color: #2e7d32;
        color: white;
        border: 1px solid #388e3c;
        border-radius: 6px;
        padding: 7px 18px;
        font-weight: bold;
        font-size: 12px;
    }
    QPushButton:hover {
        background-color: #388e3c;
    }
    QPushButton:pressed {
        background-color: #1b5e20;
    }
"""

_CLEAR_BTN_STYLE = """
    QPushButton {
        background-color: #424242;
        color: #ef9a9a;
        border: 1px solid #616161;
        border-radius: 5px;
        padding: 6px 14px;
        font-size: 12px;
    }
    QPushButton:hover {
        background-color: #c62828;
        color: white;
        border-color: #d32f2f;
    }
"""

_DONE_BTN_STYLE = """
    QPushButton {
        background-color: #007acc;
        color: white;
        border: 1px solid #0098ff;
        border-radius: 6px;
        padding: 8px 24px;
        font-weight: bold;
        font-size: 13px;
    }
    QPushButton:hover {
        background-color: #1f8ad2;
    }
"""


class _CropGraphicsView(QGraphicsView):
    """View with interactive line-dragging for header and footer crops."""

    line_dragged = Signal(str, float)  # ('header' or 'footer', y_pt)
    drag_finished = Signal()
    zoom_requested = Signal(float)
    page_nav_requested = Signal(int)  # +1 or -1

    def __init__(self, scene: QGraphicsScene, parent: QWidget | None = None) -> None:
        super().__init__(scene, parent)
        self.setMouseTracking(True)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        self._dragging_target: str | None = None  # 'header', 'footer', or None
        self._page_w_px: float = 0.0
        self._page_h_px: float = 0.0
        self._header_y_px: float = 0.0
        self._footer_y_px: float = 0.0

    def set_bounds(
        self, page_w_px: float, page_h_px: float, header_y_px: float, footer_y_px: float
    ) -> None:
        self._page_w_px = page_w_px
        self._page_h_px = page_h_px
        self._header_y_px = header_y_px
        self._footer_y_px = footer_y_px

    def _is_near_header(self, view_pos) -> bool:
        if self._page_h_px <= 0:
            return False
        header_view_y = self.mapFromScene(0, self._header_y_px).y()
        return abs(view_pos.y() - header_view_y) <= 14

    def _is_near_footer(self, view_pos) -> bool:
        if self._page_h_px <= 0:
            return False
        footer_view_y = self.mapFromScene(0, self._footer_y_px).y()
        return abs(view_pos.y() - footer_view_y) <= 14

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._page_h_px > 0:
            view_pos = event.pos()
            scene_pos = self.mapToScene(view_pos)

            # 1. Exact line proximity (within 14 screen pixels)
            if self._is_near_header(view_pos):
                self._dragging_target = "header"
                event.accept()
                return
            if self._is_near_footer(view_pos):
                self._dragging_target = "footer"
                event.accept()
                return

            # 2. Click in header / footer territory to snap line
            if 0 <= scene_pos.x() <= self._page_w_px:
                if scene_pos.y() < self._page_h_px * 0.4:
                    self._dragging_target = "header"
                    new_pt = max(0.0, min(scene_pos.y() / RENDER_DPI_SCALE, (self._footer_y_px / RENDER_DPI_SCALE) - 10))
                    self.line_dragged.emit("header", new_pt)
                    event.accept()
                    return
                elif scene_pos.y() > self._page_h_px * 0.6:
                    self._dragging_target = "footer"
                    page_h_pt = self._page_h_px / RENDER_DPI_SCALE
                    new_pt = max((self._header_y_px / RENDER_DPI_SCALE) + 10, min(scene_pos.y() / RENDER_DPI_SCALE, page_h_pt))
                    self.line_dragged.emit("footer", new_pt)
                    event.accept()
                    return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        view_pos = event.pos()
        scene_pos = self.mapToScene(view_pos)

        if self._dragging_target:
            page_h_pt = self._page_h_px / RENDER_DPI_SCALE
            y_pt = scene_pos.y() / RENDER_DPI_SCALE

            if self._dragging_target == "header":
                footer_pt = self._footer_y_px / RENDER_DPI_SCALE
                clamped_pt = max(0.0, min(y_pt, footer_pt - 10.0))
                self.line_dragged.emit("header", clamped_pt)
            elif self._dragging_target == "footer":
                header_pt = self._header_y_px / RENDER_DPI_SCALE
                clamped_pt = max(header_pt + 10.0, min(y_pt, page_h_pt))
                self.line_dragged.emit("footer", clamped_pt)

            self.setCursor(QCursor(Qt.CursorShape.SizeVerCursor))
            event.accept()
            return

        # Hover cursor
        if self._is_near_header(view_pos) or self._is_near_footer(view_pos):
            self.setCursor(QCursor(Qt.CursorShape.SizeVerCursor))
        else:
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_target:
            self._dragging_target = None
            self.drag_finished.emit()
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                self.zoom_requested.emit(1.0 + (delta / 120.0) * 0.15)
                event.accept()
                return
        else:
            delta = event.angleDelta().y()
            if abs(delta) >= 60:
                self.page_nav_requested.emit(-1 if delta > 0 else 1)
                event.accept()
                return

        super().wheelEvent(event)


class CropDialog(QDialog):
    """High-performance modal dialog for cropping header/footer regions."""

    def __init__(
        self,
        original_path: str | None,
        suspect_path: str | None,
        existing_crops: dict | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Crop Header / Footer")
        self.setStyleSheet(_DIALOG_STYLE)
        self.setMinimumSize(950, 720)

        if parent:
            self.resize(min(1280, parent.width() - 20), min(900, parent.height() - 20))
        else:
            self.resize(1180, 820)

        self._original_path = original_path
        self._suspect_path = suspect_path
        self._active_doc: str = "original"

        # Crop data: {doc_path: {page_num(1-indexed): (header_bottom_pt, footer_top_pt)}}
        self._crop_data: dict[str, dict[int, tuple[float, float]]] = {}
        if existing_crops:
            self._crop_data = {k: dict(v) for k, v in existing_crops.items()}

        # Fast document metadata & cache: path -> dict
        # {actual_path, page_count, heights_pt, widths_pt}
        self._doc_meta: dict[str, dict] = {}
        self._page_cache: dict[tuple[str, int], QPixmap] = {}

        self._current_page: int = 1
        self._zoom_factor: float = 1.0

        # Fast init: load document metadata in ~2ms (zero upfront page rendering)
        if original_path:
            self._load_doc_metadata(original_path)
        if suspect_path:
            self._load_doc_metadata(suspect_path)

        # Build UI layout
        self._build_ui()

        # Switch to first available document
        if original_path and original_path in self._doc_meta:
            self._switch_to("original")
        elif suspect_path and suspect_path in self._doc_meta:
            self._switch_to("suspect")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_page()

    # ------------------------------------------------------------------
    # Document Metadata & Lazy Rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_pdf_path(path: str) -> str | None:
        """Resolve a document path to a readable PDF path."""
        if not path or not os.path.exists(path):
            return None
        if path.lower().endswith(".pdf"):
            return path
        # Word / Office document: check cached conversion
        try:
            from core.office_convert import cached_pdf_path
            converted = cached_pdf_path(path)
            if os.path.exists(converted):
                return converted
        except Exception:
            pass
        return None

    def _load_doc_metadata(self, path: str) -> None:
        """Extract page count and dimensions in ~2ms without rendering pixels."""
        actual_path = self._resolve_pdf_path(path)
        if not actual_path:
            logger.info("CropDialog: Cannot resolve readable PDF for %s", path)
            return

        try:
            doc = fitz.open(actual_path)
            count = doc.page_count
            if count == 0:
                doc.close()
                return

            heights_pt: list[float] = []
            widths_pt: list[float] = []
            for i in range(count):
                r = doc.load_page(i).rect
                heights_pt.append(float(r.height))
                widths_pt.append(float(r.width))
            doc.close()

            self._doc_meta[path] = {
                "actual_path": actual_path,
                "page_count": count,
                "heights_pt": heights_pt,
                "widths_pt": widths_pt,
            }
        except Exception as exc:
            logger.error("CropDialog: Failed loading metadata for %s: %s", path, exc)

    def _get_page_pixmap(self, path: str, page_num: int) -> QPixmap | None:
        """Fetch pixmap from cache or render on demand in ~15ms."""
        key = (path, page_num)
        if key in self._page_cache:
            return self._page_cache[key]

        meta = self._doc_meta.get(path)
        if not meta or page_num < 1 or page_num > meta["page_count"]:
            return None

        try:
            doc = fitz.open(meta["actual_path"])
            page = doc.load_page(page_num - 1)
            mat = fitz.Matrix(RENDER_DPI_SCALE, RENDER_DPI_SCALE)
            pix = page.get_pixmap(matrix=mat)
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
            pm = QPixmap.fromImage(img)
            doc.close()

            self._page_cache[key] = pm
            return pm
        except Exception as exc:
            logger.error("CropDialog: Failed to render page %d of %s: %s", page_num, path, exc)
            return None

    # ------------------------------------------------------------------
    # UI Layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(14, 10, 14, 10)

        # 1. Top Document Switcher Bar
        top_bar = QHBoxLayout()
        top_bar.setSpacing(10)

        self._orig_btn = QPushButton("Original")
        self._orig_btn.setFixedWidth(160)
        self._orig_btn.clicked.connect(lambda: self._switch_to("original"))

        self._susp_btn = QPushButton("Suspect")
        self._susp_btn.setFixedWidth(160)
        self._susp_btn.clicked.connect(lambda: self._switch_to("suspect"))

        if not self._original_path or self._original_path not in self._doc_meta:
            self._orig_btn.setEnabled(False)
        else:
            cnt = self._doc_meta[self._original_path]["page_count"]
            self._orig_btn.setText(f"Original ({cnt} pgs)")

        if not self._suspect_path or self._suspect_path not in self._doc_meta:
            self._susp_btn.setEnabled(False)
        else:
            cnt = self._doc_meta[self._suspect_path]["page_count"]
            self._susp_btn.setText(f"Suspect ({cnt} pgs)")

        top_bar.addWidget(self._orig_btn)
        top_bar.addWidget(self._susp_btn)
        top_bar.addSpacing(16)

        self._tip_label = QLabel(
            "💡 <b>Drag the red dotted lines</b> on the page to adjust Header and Footer, then click <b>Apply to All Pages</b>."
        )
        self._tip_label.setStyleSheet("color: #90caf9; font-size: 12px;")
        top_bar.addWidget(self._tip_label, stretch=1)

        layout.addLayout(top_bar)

        # 2. Control Toolbar (Navigation, Crop Spinboxes, Zoom)
        ctrl_bar = QHBoxLayout()
        ctrl_bar.setSpacing(8)

        # Navigation
        self._prev_btn = QPushButton("‹ Prev")
        self._prev_btn.setFixedWidth(65)
        self._prev_btn.clicked.connect(self._go_prev)

        self._page_label = QLabel("Page 1 / 1")
        self._page_label.setMinimumWidth(85)
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._next_btn = QPushButton("Next ›")
        self._next_btn.setFixedWidth(65)
        self._next_btn.clicked.connect(self._go_next)

        ctrl_bar.addWidget(self._prev_btn)
        ctrl_bar.addWidget(self._page_label)
        ctrl_bar.addWidget(self._next_btn)
        ctrl_bar.addSpacing(16)

        # Header Crop Spinbox
        h_label = QLabel("<b>Header Crop:</b>")
        h_label.setStyleSheet("color: #ff8a80;")
        self._header_spin = QDoubleSpinBox()
        self._header_spin.setRange(0.0, 1000.0)
        self._header_spin.setDecimals(1)
        self._header_spin.setSingleStep(5.0)
        self._header_spin.setSuffix(" pt")
        self._header_spin.setFixedWidth(95)
        self._header_spin.valueChanged.connect(self._on_header_spin_changed)

        self._clear_h_btn = QPushButton("Clear")
        self._clear_h_btn.setFixedWidth(50)
        self._clear_h_btn.setStyleSheet(_CLEAR_BTN_STYLE)
        self._clear_h_btn.clicked.connect(self._clear_header)

        ctrl_bar.addWidget(h_label)
        ctrl_bar.addWidget(self._header_spin)
        ctrl_bar.addWidget(self._clear_h_btn)
        ctrl_bar.addSpacing(14)

        # Footer Crop Spinbox
        f_label = QLabel("<b>Footer Crop:</b>")
        f_label.setStyleSheet("color: #ff8a80;")
        self._footer_spin = QDoubleSpinBox()
        self._footer_spin.setRange(0.0, 1000.0)
        self._footer_spin.setDecimals(1)
        self._footer_spin.setSingleStep(5.0)
        self._footer_spin.setSuffix(" pt")
        self._footer_spin.setFixedWidth(95)
        self._footer_spin.valueChanged.connect(self._on_footer_spin_changed)

        self._clear_f_btn = QPushButton("Clear")
        self._clear_f_btn.setFixedWidth(50)
        self._clear_f_btn.setStyleSheet(_CLEAR_BTN_STYLE)
        self._clear_f_btn.clicked.connect(self._clear_footer)

        ctrl_bar.addWidget(f_label)
        ctrl_bar.addWidget(self._footer_spin)
        ctrl_bar.addWidget(self._clear_f_btn)
        ctrl_bar.addStretch()

        # Zoom controls
        zoom_out = QPushButton("−")
        zoom_out.setFixedWidth(32)
        zoom_out.clicked.connect(lambda: self._set_zoom(self._zoom_factor / 1.2))

        self._zoom_label = QLabel("100%")
        self._zoom_label.setFixedWidth(46)
        self._zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        zoom_in = QPushButton("+")
        zoom_in.setFixedWidth(32)
        zoom_in.clicked.connect(lambda: self._set_zoom(self._zoom_factor * 1.2))

        fit_w_btn = QPushButton("Fit Width")
        fit_w_btn.setFixedWidth(75)
        fit_w_btn.clicked.connect(self._fit_width)

        fit_p_btn = QPushButton("Fit Page")
        fit_p_btn.setFixedWidth(75)
        fit_p_btn.clicked.connect(self._fit_page)

        ctrl_bar.addWidget(zoom_out)
        ctrl_bar.addWidget(self._zoom_label)
        ctrl_bar.addWidget(zoom_in)
        ctrl_bar.addWidget(fit_w_btn)
        ctrl_bar.addWidget(fit_p_btn)

        layout.addLayout(ctrl_bar)

        # 3. Viewer Scene & Canvas
        self._scene = QGraphicsScene(self)
        self._view = _CropGraphicsView(self._scene, self)
        self._view.line_dragged.connect(self._on_line_dragged)
        self._view.drag_finished.connect(self._on_drag_finished)
        self._view.zoom_requested.connect(lambda r: self._set_zoom(self._zoom_factor * r))
        self._view.page_nav_requested.connect(self._on_page_nav_requested)

        layout.addWidget(self._view, stretch=1)

        # 4. Action Buttons Bar
        action_bar = QHBoxLayout()
        action_bar.setSpacing(10)

        self._apply_all_btn = QPushButton("✓ Apply to All Pages")
        self._apply_all_btn.setStyleSheet(_APPLY_ALL_BTN_STYLE)
        self._apply_all_btn.setToolTip("Apply this Header and Footer crop to all pages in this document.")
        self._apply_all_btn.clicked.connect(self._apply_to_all_pages)

        self._apply_page_btn = QPushButton("Apply to This Page Only")
        self._apply_page_btn.clicked.connect(self._apply_to_current_page)

        self._clear_all_btn = QPushButton("Clear All Crops")
        self._clear_all_btn.setStyleSheet(_CLEAR_BTN_STYLE)
        self._clear_all_btn.clicked.connect(self._clear_all_crops)

        action_bar.addWidget(self._apply_all_btn)
        action_bar.addWidget(self._apply_page_btn)
        action_bar.addSpacing(12)
        action_bar.addWidget(self._clear_all_btn)
        action_bar.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        cancel_btn.setFixedWidth(80)

        self._done_btn = QPushButton("Done - Save Crops")
        self._done_btn.setStyleSheet(_DONE_BTN_STYLE)
        self._done_btn.clicked.connect(self.accept)

        action_bar.addWidget(cancel_btn)
        action_bar.addWidget(self._done_btn)

        layout.addLayout(action_bar)

        # Scene graphic items
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._border_item: QGraphicsRectItem | None = None
        self._header_rect: QGraphicsRectItem | None = None
        self._header_line: QGraphicsLineItem | None = None
        self._header_badge: QGraphicsRectItem | None = None
        self._header_badge_text: QGraphicsSimpleTextItem | None = None
        self._footer_rect: QGraphicsRectItem | None = None
        self._footer_line: QGraphicsLineItem | None = None
        self._footer_badge: QGraphicsRectItem | None = None
        self._footer_badge_text: QGraphicsSimpleTextItem | None = None

        self._block_spin_signals = False

    # ------------------------------------------------------------------
    # Document Switching & Page Display
    # ------------------------------------------------------------------

    def _current_path(self) -> str | None:
        return self._original_path if self._active_doc == "original" else self._suspect_path

    def _switch_to(self, which: str) -> None:
        self._active_doc = which
        if which == "original":
            self._orig_btn.setStyleSheet(_ACTIVE_DOC_BTN)
            self._susp_btn.setStyleSheet(_INACTIVE_DOC_BTN)
        else:
            self._orig_btn.setStyleSheet(_INACTIVE_DOC_BTN)
            self._susp_btn.setStyleSheet(_ACTIVE_DOC_BTN)

        path = self._current_path()
        if not path or path not in self._doc_meta:
            self._scene.clear()
            self._page_label.setText("No document")
            return

        self._current_page = 1
        self._render_current_page()

    def _render_current_page(self) -> None:
        """Display current page with active header and footer dotted lines."""
        path = self._current_path()
        meta = self._doc_meta.get(path or "")
        if not meta:
            return

        total_pages = meta["page_count"]
        self._page_label.setText(f"Page {self._current_page} / {total_pages}")
        self._prev_btn.setEnabled(self._current_page > 1)
        self._next_btn.setEnabled(self._current_page < total_pages)

        # Get page dimensions
        page_h_pt = meta["heights_pt"][self._current_page - 1]
        page_w_pt = meta["widths_pt"][self._current_page - 1]
        page_w_px = page_w_pt * RENDER_DPI_SCALE
        page_h_px = page_h_pt * RENDER_DPI_SCALE

        # Clear old items
        self._scene.clear()

        # Render page pixmap on-demand
        pm = self._get_page_pixmap(path, self._current_page)
        if pm:
            self._pixmap_item = self._scene.addPixmap(pm)
            self._pixmap_item.setPos(0, 0)
            self._pixmap_item.setZValue(0)

        # White background and page border
        self._border_item = self._scene.addRect(
            QRectF(0, 0, page_w_px, page_h_px),
            QPen(QColor(80, 80, 80), 1),
            Qt.BrushStyle.NoBrush,
        )
        self._border_item.setZValue(1)

        # Scene bounding rect with margin
        self._scene.setSceneRect(-30, -30, page_w_px + 60, page_h_px + 60)

        # Get existing crop for this page (or default 0.0, page_h_pt)
        page_crops = self._crop_data.get(path, {})
        header_pt, footer_pt = page_crops.get(self._current_page, (0.0, page_h_pt))
        header_pt = max(0.0, min(header_pt, page_h_pt))
        footer_pt = max(0.0, min(footer_pt, page_h_pt))

        # Update spinboxes
        self._block_spin_signals = True
        self._header_spin.setMaximum(page_h_pt)
        self._header_spin.setValue(header_pt)
        self._footer_spin.setMaximum(page_h_pt)
        footer_from_bot = max(0.0, page_h_pt - footer_pt)
        self._footer_spin.setValue(footer_from_bot)
        self._block_spin_signals = False

        # Build interactive graphic items
        self._build_overlay_items(page_w_px, page_h_px, header_pt, footer_pt, page_h_pt)

    def _build_overlay_items(
        self,
        page_w_px: float,
        page_h_px: float,
        header_pt: float,
        footer_pt: float,
        page_h_pt: float,
    ) -> None:
        """Create both Header and Footer dotted lines and translucent bands."""
        h_y_px = header_pt * RENDER_DPI_SCALE
        f_y_px = footer_pt * RENDER_DPI_SCALE

        # 1. Header Exclusion Rect
        self._header_rect = self._scene.addRect(
            QRectF(0, 0, page_w_px, h_y_px),
            Qt.PenStyle.NoPen,
            _CROP_OVERLAY_BRUSH,
        )
        self._header_rect.setZValue(10)

        # 2. Header Dotted Line
        self._header_line = self._scene.addLine(0, h_y_px, page_w_px, h_y_px, _HEADER_PEN)
        self._header_line.setZValue(12)

        # 3. Header Handle Badge
        self._header_badge, self._header_badge_text = self._create_badge(
            text=f"Header: {header_pt:.0f} pt  ↕ drag",
            y_px=h_y_px,
            is_header=True,
        )

        # 4. Footer Exclusion Rect
        self._footer_rect = self._scene.addRect(
            QRectF(0, f_y_px, page_w_px, page_h_px - f_y_px),
            Qt.PenStyle.NoPen,
            _CROP_OVERLAY_BRUSH,
        )
        self._footer_rect.setZValue(10)

        # 5. Footer Dotted Line
        self._footer_line = self._scene.addLine(0, f_y_px, page_w_px, f_y_px, _FOOTER_PEN)
        self._footer_line.setZValue(12)

        # 6. Footer Handle Badge
        footer_from_bot = max(0.0, page_h_pt - footer_pt)
        self._footer_badge, self._footer_badge_text = self._create_badge(
            text=f"Footer: {footer_from_bot:.0f} pt  ↕ drag",
            y_px=f_y_px,
            is_header=False,
        )

        # Update view bounds for mouse dragging
        self._view.set_bounds(page_w_px, page_h_px, h_y_px, f_y_px)

    def _create_badge(
        self, text: str, y_px: float, is_header: bool
    ) -> tuple[QGraphicsRectItem, QGraphicsSimpleTextItem]:
        badge_w, badge_h = 160.0, 22.0
        badge_x = 24.0
        badge_y = y_px - badge_h if is_header else y_px

        rect_item = self._scene.addRect(
            QRectF(badge_x, badge_y, badge_w, badge_h),
            _HANDLE_BORDER_PEN,
            _HANDLE_BG_BRUSH,
        )
        rect_item.setZValue(15)

        text_item = self._scene.addSimpleText(text)
        font = QFont("Segoe UI", 8, QFont.Weight.Bold)
        text_item.setFont(font)
        text_item.setBrush(QBrush(QColor(255, 255, 255)))
        text_item.setPos(badge_x + 8, badge_y + 3)
        text_item.setZValue(16)

        return rect_item, text_item

    # ------------------------------------------------------------------
    # Dynamic Dotted Line Dragging Updates
    # ------------------------------------------------------------------

    def _on_line_dragged(self, which: str, val_pt: float) -> None:
        path = self._current_path()
        meta = self._doc_meta.get(path or "")
        if not meta:
            return

        page_h_pt = meta["heights_pt"][self._current_page - 1]
        page_w_px = meta["widths_pt"][self._current_page - 1] * RENDER_DPI_SCALE
        page_h_px = page_h_pt * RENDER_DPI_SCALE

        page_crops = self._crop_data.setdefault(path, {})
        cur_header_pt, cur_footer_pt = page_crops.get(self._current_page, (0.0, page_h_pt))

        if which == "header":
            header_pt = max(0.0, min(val_pt, cur_footer_pt - 5.0))
            page_crops[self._current_page] = (header_pt, cur_footer_pt)

            h_y_px = header_pt * RENDER_DPI_SCALE
            if self._header_rect:
                self._header_rect.setRect(0, 0, page_w_px, h_y_px)
            if self._header_line:
                self._header_line.setLine(0, h_y_px, page_w_px, h_y_px)
            if self._header_badge and self._header_badge_text:
                badge_h = 22.0
                by = max(0.0, h_y_px - badge_h)
                self._header_badge.setRect(24.0, by, 160.0, badge_h)
                self._header_badge_text.setPos(32.0, by + 3)
                self._header_badge_text.setText(f"Header: {header_pt:.0f} pt  ↕ drag")

            self._block_spin_signals = True
            self._header_spin.setValue(header_pt)
            self._block_spin_signals = False

        elif which == "footer":
            footer_pt = max(cur_header_pt + 5.0, min(val_pt, page_h_pt))
            page_crops[self._current_page] = (cur_header_pt, footer_pt)

            f_y_px = footer_pt * RENDER_DPI_SCALE
            if self._footer_rect:
                self._footer_rect.setRect(0, f_y_px, page_w_px, page_h_px - f_y_px)
            if self._footer_line:
                self._footer_line.setLine(0, f_y_px, page_w_px, f_y_px)
            if self._footer_badge and self._footer_badge_text:
                badge_h = 22.0
                by = min(page_h_px - badge_h, f_y_px)
                self._footer_badge.setRect(24.0, by, 160.0, badge_h)
                self._footer_badge_text.setPos(32.0, by + 3)
                footer_from_bot = max(0.0, page_h_pt - footer_pt)
                self._footer_badge_text.setText(f"Footer: {footer_from_bot:.0f} pt  ↕ drag")

            self._block_spin_signals = True
            footer_from_bot = max(0.0, page_h_pt - footer_pt)
            self._footer_spin.setValue(footer_from_bot)
            self._block_spin_signals = False

        # Update view bounds
        h_pt, f_pt = page_crops[self._current_page]
        self._view.set_bounds(page_w_px, page_h_px, h_pt * RENDER_DPI_SCALE, f_pt * RENDER_DPI_SCALE)

    def _on_drag_finished(self) -> None:
        path = self._current_path()
        if not path:
            return
        meta = self._doc_meta.get(path)
        if not meta:
            return
        page_h_pt = meta["heights_pt"][self._current_page - 1]
        page_crops = self._crop_data.get(path, {})
        h_pt, f_pt = page_crops.get(self._current_page, (0.0, page_h_pt))
        f_from_bot = max(0.0, page_h_pt - f_pt)
        self._tip_label.setText(
            f"Page {self._current_page}: Header <b>{h_pt:.0f} pt</b>, Footer <b>{f_from_bot:.0f} pt</b>. "
            f"Click <b>Apply to All Pages</b> to set across all pages."
        )

    # ------------------------------------------------------------------
    # Spinbox Event Handlers
    # ------------------------------------------------------------------

    def _on_header_spin_changed(self, val: float) -> None:
        if self._block_spin_signals:
            return
        self._on_line_dragged("header", val)

    def _on_footer_spin_changed(self, val_from_bottom: float) -> None:
        if self._block_spin_signals:
            return
        path = self._current_path()
        meta = self._doc_meta.get(path or "")
        if not meta:
            return
        page_h_pt = meta["heights_pt"][self._current_page - 1]
        footer_pt = max(0.0, page_h_pt - val_from_bottom)
        self._on_line_dragged("footer", footer_pt)

    def _clear_header(self) -> None:
        self._on_line_dragged("header", 0.0)

    def _clear_footer(self) -> None:
        path = self._current_path()
        meta = self._doc_meta.get(path or "")
        if not meta:
            return
        page_h_pt = meta["heights_pt"][self._current_page - 1]
        self._on_line_dragged("footer", page_h_pt)

    # ------------------------------------------------------------------
    # Apply Actions
    # ------------------------------------------------------------------

    def _apply_to_all_pages(self) -> None:
        """Apply current header & footer margins to ALL pages of this document."""
        path = self._current_path()
        meta = self._doc_meta.get(path or "")
        if not meta:
            return

        page_h_pt = meta["heights_pt"][self._current_page - 1]
        page_crops = self._crop_data.setdefault(path, {})
        cur_header_pt, cur_footer_pt = page_crops.get(self._current_page, (0.0, page_h_pt))
        footer_margin_pt = max(0.0, page_h_pt - cur_footer_pt)

        total_pages = meta["page_count"]
        for p in range(1, total_pages + 1):
            p_height = meta["heights_pt"][p - 1]
            p_footer_pt = max(0.0, p_height - footer_margin_pt)
            page_crops[p] = (cur_header_pt, p_footer_pt)

        self._tip_label.setText(
            f"✅ <b>Applied to all {total_pages} pages:</b> Header {cur_header_pt:.0f} pt, "
            f"Footer {footer_margin_pt:.0f} pt from bottom."
        )

    def _apply_to_current_page(self) -> None:
        self._tip_label.setText(f"✅ Saved crop for page {self._current_page}.")

    def _clear_all_crops(self) -> None:
        path = self._current_path()
        if path and path in self._crop_data:
            self._crop_data[path] = {}
        self._render_current_page()
        self._tip_label.setText("Crops cleared for this document.")

    # ------------------------------------------------------------------
    # Navigation & Zoom
    # ------------------------------------------------------------------

    def _go_prev(self) -> None:
        if self._current_page > 1:
            self._current_page -= 1
            self._render_current_page()

    def _go_next(self) -> None:
        path = self._current_path()
        meta = self._doc_meta.get(path or "")
        if meta and self._current_page < meta["page_count"]:
            self._current_page += 1
            self._render_current_page()

    def _on_page_nav_requested(self, delta: int) -> None:
        if delta < 0:
            self._go_prev()
        elif delta > 0:
            self._go_next()

    def _set_zoom(self, factor: float) -> None:
        self._zoom_factor = max(0.25, min(3.0, factor))
        self._zoom_label.setText(f"{int(self._zoom_factor * 100)}%")
        self._view.resetTransform()
        self._view.scale(self._zoom_factor, self._zoom_factor)

    def _fit_width(self) -> None:
        path = self._current_path()
        meta = self._doc_meta.get(path or "")
        if not meta:
            return
        page_w_px = meta["widths_pt"][self._current_page - 1] * RENDER_DPI_SCALE
        viewport_w = self._view.viewport().width() - 50
        if page_w_px > 0 and viewport_w > 0:
            self._set_zoom(viewport_w / page_w_px)

    def _fit_page(self) -> None:
        path = self._current_path()
        meta = self._doc_meta.get(path or "")
        if not meta:
            return
        page_w_px = meta["widths_pt"][self._current_page - 1] * RENDER_DPI_SCALE
        page_h_px = meta["heights_pt"][self._current_page - 1] * RENDER_DPI_SCALE
        viewport_w = self._view.viewport().width() - 50
        viewport_h = self._view.viewport().height() - 50
        if page_w_px > 0 and page_h_px > 0 and viewport_w > 0 and viewport_h > 0:
            scale = min(viewport_w / page_w_px, viewport_h / page_h_px)
            self._set_zoom(scale)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_crop_data(self) -> dict[str, dict[int, tuple[float, float]]]:
        """Return clean crop data for both documents."""
        result: dict[str, dict[int, tuple[float, float]]] = {}
        for path, pages in self._crop_data.items():
            meta = self._doc_meta.get(path)
            clean_pages = {}
            for p, (h, f) in pages.items():
                p_h = meta["heights_pt"][p - 1] if meta and p <= len(meta["heights_pt"]) else 800.0
                # Keep only meaningful crops
                if h > 0.0 or f < p_h:
                    clean_pages[p] = (h, f)
            if clean_pages:
                result[path] = clean_pages
        return result
