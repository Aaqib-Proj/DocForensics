"""crop_dialog.py -- Visual Header, Footer, and Side Crop Exclusion Dialog.

Allows reviewers to define header, footer, left, and right margins to exclude from comparison:
- Ultra-fast opening via metadata caching
- Drag-and-drop interactive dotted lines with SizeVerCursor and SizeHorCursor
- Exclusion bands with semi-transparent overlay
- Numeric point spinboxes with Clear buttons
- Single-page vs All-pages application
- Seamless document switching between Original and Suspect
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import fitz  # PyMuPDF
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui import theme

logger = logging.getLogger(__name__)

RENDER_DPI_SCALE = 200 / 72

# Crop styling tokens
_HEADER_PEN = QPen(QColor(244, 67, 54, 240), 2, Qt.PenStyle.DashLine)
_HEADER_PEN.setCosmetic(True)

_FOOTER_PEN = QPen(QColor(244, 67, 54, 240), 2, Qt.PenStyle.DashLine)
_FOOTER_PEN.setCosmetic(True)

_LEFT_PEN = QPen(QColor(244, 67, 54, 240), 2, Qt.PenStyle.DashLine)
_LEFT_PEN.setCosmetic(True)

_RIGHT_PEN = QPen(QColor(244, 67, 54, 240), 2, Qt.PenStyle.DashLine)
_RIGHT_PEN.setCosmetic(True)

_CROP_OVERLAY_BRUSH = QBrush(QColor(220, 40, 40, 65))
_HANDLE_BG_BRUSH = QBrush(QColor(198, 40, 40, 230))
_HANDLE_BORDER_PEN = QPen(QColor(255, 255, 255, 210), 1)
_HANDLE_BORDER_PEN.setCosmetic(True)

# Styles follow the app theme (ui/theme.py): the dialog used to be dark
# whatever the theme, and stood out from everything else in light mode.
# Why a loaded document has no pages here.
_NOT_CROPPABLE = (
    "This document has no fixed pages to crop: a PowerPoint or Excel file, or a "
    "Word document shown in its approximate layout because neither Microsoft Word "
    "nor LibreOffice is installed."
)


def _dialog_style(t) -> str:
    return f"""
    QDoubleSpinBox {{ font-weight: 600; }}
    QGraphicsView {{ border: 1px solid {t.border}; border-radius: 6px; background-color: {t.canvas}; }}
"""


def _active_doc_btn(t) -> str:
    return f"""
    QPushButton {{ background-color: {t.accent}; color: {t.accent_text}; border: 1px solid {t.accent};
        border-radius: 6px; padding: 6px 16px; font-weight: 600; }}
"""


def _inactive_doc_btn(t) -> str:
    return f"""
    QPushButton {{ background-color: {t.surface}; color: {t.muted}; border: 1px solid {t.border_strong};
        border-radius: 6px; padding: 6px 16px; }}
    QPushButton:hover {{ background-color: {t.hover}; color: {t.text}; }}
"""


def _apply_all_btn(t) -> str:
    return f"""
    QPushButton {{ background-color: {t.success}; color: #ffffff; border: 1px solid {t.success};
        border-radius: 6px; padding: 6px 16px; font-weight: 600; }}
    QPushButton:hover {{ background-color: {t.success}; border-color: {t.text}; }}
"""


def _clear_btn(t) -> str:
    return f"""
    QPushButton {{ background-color: transparent; color: {t.danger}; border: 1px solid {t.border_strong};
        border-radius: 6px; padding: 4px 8px; }}
    QPushButton:hover {{ background-color: {t.danger}; color: #ffffff; border-color: {t.danger}; }}
    QPushButton:disabled {{ color: {t.border_strong}; background-color: transparent; }}
"""


class _CropGraphicsView(QGraphicsView):
    """View with interactive line-dragging for header, footer, and side crops."""

    line_dragged = Signal(str, float)  # ('header'|'footer'|'left'|'right', val_pt)
    drag_finished = Signal()
    zoom_requested = Signal(float)
    page_nav_requested = Signal(int)  # +1 or -1

    def __init__(self, scene: QGraphicsScene, parent: QWidget | None = None) -> None:
        super().__init__(scene, parent)
        self.setMouseTracking(True)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        self._dragging_target: str | None = None  # 'header', 'footer', 'left', 'right', or None
        self._page_w_px: float = 0.0
        self._page_h_px: float = 0.0
        self._header_y_px: float = 0.0
        self._footer_y_px: float = 0.0
        self._left_x_px: float = 0.0
        self._right_x_px: float = 0.0

    def set_bounds(
        self,
        page_w_px: float,
        page_h_px: float,
        header_y_px: float,
        footer_y_px: float,
        left_x_px: float,
        right_x_px: float,
    ) -> None:
        self._page_w_px = page_w_px
        self._page_h_px = page_h_px
        self._header_y_px = header_y_px
        self._footer_y_px = footer_y_px
        self._left_x_px = left_x_px
        self._right_x_px = right_x_px

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

    def _is_near_left(self, view_pos) -> bool:
        if self._page_w_px <= 0:
            return False
        left_view_x = self.mapFromScene(self._left_x_px, 0).x()
        return abs(view_pos.x() - left_view_x) <= 14

    def _is_near_right(self, view_pos) -> bool:
        if self._page_w_px <= 0:
            return False
        right_view_x = self.mapFromScene(self._right_x_px, 0).x()
        return abs(view_pos.x() - right_view_x) <= 14

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._page_h_px > 0 and self._page_w_px > 0:
            view_pos = event.pos()
            scene_pos = self.mapToScene(view_pos)

            # 1. Exact line proximity (within 14 screen pixels)
            d_h = abs(view_pos.y() - self.mapFromScene(0, self._header_y_px).y())
            d_f = abs(view_pos.y() - self.mapFromScene(0, self._footer_y_px).y())
            d_l = abs(view_pos.x() - self.mapFromScene(self._left_x_px, 0).x())
            d_r = abs(view_pos.x() - self.mapFromScene(self._right_x_px, 0).x())

            candidates = [("header", d_h), ("footer", d_f), ("left", d_l), ("right", d_r)]
            candidates.sort(key=lambda c: c[1])
            best_target, best_dist = candidates[0]
            if best_dist <= 14:
                self._dragging_target = best_target
                event.accept()
                return

            # 2. Click in margin territory to snap line
            if 0 <= scene_pos.x() <= self._page_w_px and 0 <= scene_pos.y() <= self._page_h_px:
                margin_left_dist = scene_pos.x()
                margin_right_dist = self._page_w_px - scene_pos.x()
                margin_top_dist = scene_pos.y()
                margin_bot_dist = self._page_h_px - scene_pos.y()

                # Inside existing crop exclusion zones
                if scene_pos.x() <= self._left_x_px:
                    self._dragging_target = "left"
                    self.line_dragged.emit("left", max(0.0, scene_pos.x() / RENDER_DPI_SCALE))
                    event.accept()
                    return
                if scene_pos.x() >= self._right_x_px:
                    self._dragging_target = "right"
                    self.line_dragged.emit("right", min(self._page_w_px / RENDER_DPI_SCALE, scene_pos.x() / RENDER_DPI_SCALE))
                    event.accept()
                    return
                if scene_pos.y() <= self._header_y_px:
                    self._dragging_target = "header"
                    self.line_dragged.emit("header", max(0.0, scene_pos.y() / RENDER_DPI_SCALE))
                    event.accept()
                    return
                if scene_pos.y() >= self._footer_y_px:
                    self._dragging_target = "footer"
                    self.line_dragged.emit("footer", min(self._page_h_px / RENDER_DPI_SCALE, scene_pos.y() / RENDER_DPI_SCALE))
                    event.accept()
                    return

                # If near outer edges when crop line is currently at 0 or edge
                min_edge = min(margin_left_dist, margin_right_dist, margin_top_dist, margin_bot_dist)
                if min_edge == margin_left_dist and margin_left_dist <= self._page_w_px * 0.2:
                    self._dragging_target = "left"
                    new_pt = max(0.0, min(scene_pos.x() / RENDER_DPI_SCALE, (self._right_x_px / RENDER_DPI_SCALE) - 10))
                    self.line_dragged.emit("left", new_pt)
                    event.accept()
                    return
                elif min_edge == margin_right_dist and margin_right_dist <= self._page_w_px * 0.2:
                    self._dragging_target = "right"
                    page_w_pt = self._page_w_px / RENDER_DPI_SCALE
                    new_pt = max((self._left_x_px / RENDER_DPI_SCALE) + 10, min(scene_pos.x() / RENDER_DPI_SCALE, page_w_pt))
                    self.line_dragged.emit("right", new_pt)
                    event.accept()
                    return
                elif min_edge == margin_top_dist and margin_top_dist <= self._page_h_px * 0.2:
                    self._dragging_target = "header"
                    new_pt = max(0.0, min(scene_pos.y() / RENDER_DPI_SCALE, (self._footer_y_px / RENDER_DPI_SCALE) - 10))
                    self.line_dragged.emit("header", new_pt)
                    event.accept()
                    return
                elif min_edge == margin_bot_dist and margin_bot_dist <= self._page_h_px * 0.2:
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
            if self._dragging_target in ("header", "footer"):
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

            elif self._dragging_target in ("left", "right"):
                page_w_pt = self._page_w_px / RENDER_DPI_SCALE
                x_pt = scene_pos.x() / RENDER_DPI_SCALE
                if self._dragging_target == "left":
                    right_pt = self._right_x_px / RENDER_DPI_SCALE
                    clamped_pt = max(0.0, min(x_pt, right_pt - 10.0))
                    self.line_dragged.emit("left", clamped_pt)
                elif self._dragging_target == "right":
                    left_pt = self._left_x_px / RENDER_DPI_SCALE
                    clamped_pt = max(left_pt + 10.0, min(x_pt, page_w_pt))
                    self.line_dragged.emit("right", clamped_pt)
                self.setCursor(QCursor(Qt.CursorShape.SizeHorCursor))

            event.accept()
            return

        # Hover cursor
        if self._is_near_header(view_pos) or self._is_near_footer(view_pos):
            self.setCursor(QCursor(Qt.CursorShape.SizeVerCursor))
        elif self._is_near_left(view_pos) or self._is_near_right(view_pos):
            self.setCursor(QCursor(Qt.CursorShape.SizeHorCursor))
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
            if delta > 0:
                self.zoom_requested.emit(1.15)
            elif delta < 0:
                self.zoom_requested.emit(1 / 1.15)
            event.accept()
            return
        super().wheelEvent(event)


class CropDialog(QDialog):
    """Interactive dialog to preview, adjust, and apply Header, Footer, and Side margin crops."""

    def __init__(
        self,
        original_path: str | None,
        suspect_path: str | None,
        existing_crops: dict[str, dict[int, tuple]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Crop Margins (Header / Footer / Sides)")
        self.setStyleSheet(_dialog_style(theme.tokens()))
        self.setMinimumSize(980, 720)
        if parent:
            self.resize(min(1280, parent.width() - 20), min(900, parent.height() - 20))
        else:
            self.resize(1180, 820)

        self._original_path = original_path
        self._suspect_path = suspect_path

        # Cache of parsed document metadata: {path: {'actual_path': str, 'page_count': int, 'heights_pt': list[float], 'widths_pt': list[float]}}
        self._doc_meta: dict[str, dict] = {}
        # Rendered pixmaps cache: {(path, page_num): QPixmap}
        self._page_cache: dict[tuple[str, int], QPixmap] = {}

        # Pre-load metadata for both documents (instant, does not render pixels)
        if self._original_path:
            self._load_doc_metadata(self._original_path)
        if self._suspect_path:
            self._load_doc_metadata(self._suspect_path)

        # Active document: 'original' or 'suspect'
        self._active_doc: str = "original" if (self._original_path and self._original_path in self._doc_meta) else "suspect"
        self._current_page: int = 1
        self._zoom_factor: float = 1.0

        # State storage: {path: {page_num: (header_pt, footer_pt, left_pt, right_pt)}}
        self._crop_data: dict[str, dict[int, tuple[float, float, float, float]]] = {}
        if existing_crops:
            for path, pages in existing_crops.items():
                self._crop_data[path] = {}
                meta = self._doc_meta.get(path)
                for p, crop in pages.items():
                    p_w = meta["widths_pt"][p - 1] if meta and p <= len(meta["widths_pt"]) else 800.0
                    h = float(crop[0])
                    f = float(crop[1])
                    l = float(crop[2]) if len(crop) >= 4 else 0.0
                    r = float(crop[3]) if len(crop) >= 4 else p_w
                    self._crop_data[path][p] = (h, f, l, r)

        self._build_ui()
        self._render_current_page()
        self._fit_page()

    # ------------------------------------------------------------------
    # Document Metadata & Rendering
    # ------------------------------------------------------------------

    def _resolve_pdf_path(self, path: str) -> str | None:
        if not path:
            return None
        if os.path.isfile(path) and path.lower().endswith(".pdf"):
            return path
        # A Word document is shown and compared through the PDF Word (or
        # LibreOffice) laid it out as: crop that same PDF, so the margins
        # drawn here are the ones the comparison and the viewer use. (This
        # used to import a helper that did not exist, the error was
        # swallowed, and no Word document could be cropped.)
        try:
            from core.document_extractor import document_kind, layout_pdf_path
            from core.office_convert import cached_pdf_path

            if document_kind(path) != "docx":
                return None  # PowerPoint/Excel have no fixed pages to crop
            cached = cached_pdf_path(path)
            converted = cached if os.path.isfile(cached) else layout_pdf_path(path)
            if converted and os.path.isfile(converted):
                return converted
        except Exception:  # noqa: BLE001 -- shown as "cannot crop" below
            logger.exception("CropDialog: could not lay out %s", path)
        return None

    def _load_doc_metadata(self, path: str) -> None:
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
        layout.setSpacing(6)
        layout.setContentsMargins(14, 10, 14, 10)

        # 1. Top Document Switcher & Navigation & Zoom Bar
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        self._orig_btn = QPushButton("Original")
        self._orig_btn.setFixedWidth(150)
        self._orig_btn.clicked.connect(lambda: self._switch_to("original"))

        self._susp_btn = QPushButton("Suspect")
        self._susp_btn.setFixedWidth(150)
        self._susp_btn.clicked.connect(lambda: self._switch_to("suspect"))

        if not self._original_path or self._original_path not in self._doc_meta:
            self._orig_btn.setEnabled(False)
            if self._original_path:
                self._orig_btn.setToolTip(_NOT_CROPPABLE)
        else:
            cnt = self._doc_meta[self._original_path]["page_count"]
            self._orig_btn.setText(f"Original ({cnt} pgs)")

        if not self._suspect_path or self._suspect_path not in self._doc_meta:
            self._susp_btn.setEnabled(False)
            if self._suspect_path:
                self._susp_btn.setToolTip(_NOT_CROPPABLE)
        else:
            cnt = self._doc_meta[self._suspect_path]["page_count"]
            self._susp_btn.setText(f"Suspect ({cnt} pgs)")

        top_bar.addWidget(self._orig_btn)
        top_bar.addWidget(self._susp_btn)
        top_bar.addSpacing(12)

        # Page Navigation
        self._prev_btn = QPushButton("‹ Prev")
        self._prev_btn.setFixedWidth(65)
        self._prev_btn.clicked.connect(self._go_prev)

        self._page_label = QLabel("Page 1 / 1")
        self._page_label.setMinimumWidth(85)
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._next_btn = QPushButton("Next ›")
        self._next_btn.setFixedWidth(65)
        self._next_btn.clicked.connect(self._go_next)

        top_bar.addWidget(self._prev_btn)
        top_bar.addWidget(self._page_label)
        top_bar.addWidget(self._next_btn)
        top_bar.addStretch()

        # Zoom controls
        zoom_out = QPushButton("−")
        zoom_out.setFixedWidth(30)
        zoom_out.clicked.connect(lambda: self._set_zoom(self._zoom_factor / 1.2))

        self._zoom_label = QLabel("100%")
        self._zoom_label.setFixedWidth(46)
        self._zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        zoom_in = QPushButton("+")
        zoom_in.setFixedWidth(30)
        zoom_in.clicked.connect(lambda: self._set_zoom(self._zoom_factor * 1.2))

        fit_w_btn = QPushButton("Fit Width")
        fit_w_btn.setFixedWidth(75)
        fit_w_btn.clicked.connect(self._fit_width)

        fit_p_btn = QPushButton("Fit Page")
        fit_p_btn.setFixedWidth(75)
        fit_p_btn.clicked.connect(self._fit_page)

        top_bar.addWidget(zoom_out)
        top_bar.addWidget(self._zoom_label)
        top_bar.addWidget(zoom_in)
        top_bar.addWidget(fit_w_btn)
        top_bar.addWidget(fit_p_btn)

        layout.addLayout(top_bar)

        # 2. Margins Control Bar (Header, Footer, Left, Right)
        margins_bar = QHBoxLayout()
        margins_bar.setSpacing(6)

        # Header Crop Spinbox
        h_label = QLabel("<b>Header:</b>")
        h_label.setStyleSheet(f"color: {theme.tokens().danger}; font-weight: 600;")
        self._header_spin = QDoubleSpinBox()
        self._header_spin.setRange(0.0, 5000.0)
        self._header_spin.setDecimals(1)
        self._header_spin.setSingleStep(5.0)
        self._header_spin.setSuffix(" pt")
        self._header_spin.setFixedWidth(82)
        self._header_spin.valueChanged.connect(self._on_header_spin_changed)

        self._clear_h_btn = QPushButton("✕")
        self._clear_h_btn.setToolTip("Reset Header crop to 0 pt")
        self._clear_h_btn.setFixedWidth(28)
        self._clear_h_btn.setStyleSheet(_clear_btn(theme.tokens()))
        self._clear_h_btn.clicked.connect(self._clear_header)

        margins_bar.addWidget(h_label)
        margins_bar.addWidget(self._header_spin)
        margins_bar.addWidget(self._clear_h_btn)
        margins_bar.addSpacing(10)

        # Footer Crop Spinbox
        f_label = QLabel("<b>Footer:</b>")
        f_label.setStyleSheet(f"color: {theme.tokens().danger}; font-weight: 600;")
        self._footer_spin = QDoubleSpinBox()
        self._footer_spin.setRange(0.0, 5000.0)
        self._footer_spin.setDecimals(1)
        self._footer_spin.setSingleStep(5.0)
        self._footer_spin.setSuffix(" pt")
        self._footer_spin.setFixedWidth(82)
        self._footer_spin.valueChanged.connect(self._on_footer_spin_changed)

        self._clear_f_btn = QPushButton("✕")
        self._clear_f_btn.setToolTip("Reset Footer crop to 0 pt")
        self._clear_f_btn.setFixedWidth(28)
        self._clear_f_btn.setStyleSheet(_clear_btn(theme.tokens()))
        self._clear_f_btn.clicked.connect(self._clear_footer)

        margins_bar.addWidget(f_label)
        margins_bar.addWidget(self._footer_spin)
        margins_bar.addWidget(self._clear_f_btn)
        margins_bar.addSpacing(10)

        # Left Crop Spinbox
        l_label = QLabel("<b>Left:</b>")
        l_label.setStyleSheet(f"color: {theme.tokens().danger}; font-weight: 600;")
        self._left_spin = QDoubleSpinBox()
        self._left_spin.setRange(0.0, 5000.0)
        self._left_spin.setDecimals(1)
        self._left_spin.setSingleStep(5.0)
        self._left_spin.setSuffix(" pt")
        self._left_spin.setFixedWidth(82)
        self._left_spin.valueChanged.connect(self._on_left_spin_changed)

        self._clear_l_btn = QPushButton("✕")
        self._clear_l_btn.setToolTip("Reset Left side crop to 0 pt")
        self._clear_l_btn.setFixedWidth(28)
        self._clear_l_btn.setStyleSheet(_clear_btn(theme.tokens()))
        self._clear_l_btn.clicked.connect(self._clear_left)

        margins_bar.addWidget(l_label)
        margins_bar.addWidget(self._left_spin)
        margins_bar.addWidget(self._clear_l_btn)
        margins_bar.addSpacing(10)

        # Right Crop Spinbox
        r_label = QLabel("<b>Right:</b>")
        r_label.setStyleSheet(f"color: {theme.tokens().danger}; font-weight: 600;")
        self._right_spin = QDoubleSpinBox()
        self._right_spin.setRange(0.0, 5000.0)
        self._right_spin.setDecimals(1)
        self._right_spin.setSingleStep(5.0)
        self._right_spin.setSuffix(" pt")
        self._right_spin.setFixedWidth(82)
        self._right_spin.valueChanged.connect(self._on_right_spin_changed)

        self._clear_r_btn = QPushButton("✕")
        self._clear_r_btn.setToolTip("Reset Right side crop to 0 pt")
        self._clear_r_btn.setFixedWidth(28)
        self._clear_r_btn.setStyleSheet(_clear_btn(theme.tokens()))
        self._clear_r_btn.clicked.connect(self._clear_right)

        margins_bar.addWidget(r_label)
        margins_bar.addWidget(self._right_spin)
        margins_bar.addWidget(self._clear_r_btn)
        margins_bar.addSpacing(14)

        # Dynamic Tip & Info label
        self._tip_label = QLabel(
            "💡 <b>Drag the red dotted lines</b> (or use spinboxes) to set Header, Footer, and Side crops."
        )
        self._tip_label.setStyleSheet(f"color: {theme.tokens().accent};")
        margins_bar.addWidget(self._tip_label, stretch=1)

        layout.addLayout(margins_bar)

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
        self._apply_all_btn.setStyleSheet(_apply_all_btn(theme.tokens()))
        self._apply_all_btn.setToolTip("Apply these margin crops (Header, Footer, Left, Right) to all pages in this document.")
        self._apply_all_btn.clicked.connect(self._apply_to_all_pages)

        self._apply_page_btn = QPushButton("Apply to This Page Only")
        self._apply_page_btn.clicked.connect(self._apply_to_current_page)

        self._clear_page_btn = QPushButton("Clear This Page")
        self._clear_page_btn.setStyleSheet(_clear_btn(theme.tokens()))
        self._clear_page_btn.setToolTip("Clear all 4 margin crops on the current page.")
        self._clear_page_btn.clicked.connect(self._clear_current_page)

        self._clear_all_btn = QPushButton("Clear All Pages")
        self._clear_all_btn.setStyleSheet(_clear_btn(theme.tokens()))
        self._clear_all_btn.setToolTip("Clear all margin crops across all pages of this document.")
        self._clear_all_btn.clicked.connect(self._clear_all_crops)

        action_bar.addWidget(self._apply_all_btn)
        action_bar.addWidget(self._apply_page_btn)
        action_bar.addSpacing(8)
        action_bar.addWidget(self._clear_page_btn)
        action_bar.addWidget(self._clear_all_btn)
        action_bar.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        cancel_btn.setFixedWidth(80)

        self._done_btn = QPushButton("Done - Save Crops")
        self._done_btn.setObjectName("primary")
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
        self._left_rect: QGraphicsRectItem | None = None
        self._left_line: QGraphicsLineItem | None = None
        self._left_badge: QGraphicsRectItem | None = None
        self._left_badge_text: QGraphicsSimpleTextItem | None = None
        self._right_rect: QGraphicsRectItem | None = None
        self._right_line: QGraphicsLineItem | None = None
        self._right_badge: QGraphicsRectItem | None = None
        self._right_badge_text: QGraphicsSimpleTextItem | None = None

        self._block_spin_signals = False

    # ------------------------------------------------------------------
    # Document Switching & Page Display
    # ------------------------------------------------------------------

    def _current_path(self) -> str | None:
        return self._original_path if self._active_doc == "original" else self._suspect_path

    def _switch_to(self, which: str) -> None:
        self._active_doc = which
        if which == "original":
            self._orig_btn.setStyleSheet(_active_doc_btn(theme.tokens()))
            self._susp_btn.setStyleSheet(_inactive_doc_btn(theme.tokens()))
        else:
            self._orig_btn.setStyleSheet(_inactive_doc_btn(theme.tokens()))
            self._susp_btn.setStyleSheet(_active_doc_btn(theme.tokens()))

        path = self._current_path()
        if not path or path not in self._doc_meta:
            self._scene.clear()
            self._page_label.setText("No document")
            return

        self._current_page = 1
        self._render_current_page()

    def _render_current_page(self) -> None:
        """Display current page with active header, footer, left, and right dotted lines."""
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

        # Get existing crop for this page (or default 0.0, page_h_pt, 0.0, page_w_pt)
        page_crops = self._crop_data.get(path, {})
        cur_crop = page_crops.get(self._current_page, (0.0, page_h_pt, 0.0, page_w_pt))
        header_pt = max(0.0, min(float(cur_crop[0]), page_h_pt))
        footer_pt = max(0.0, min(float(cur_crop[1]), page_h_pt))
        left_pt = max(0.0, min(float(cur_crop[2]) if len(cur_crop) >= 4 else 0.0, page_w_pt))
        right_pt = max(0.0, min(float(cur_crop[3]) if len(cur_crop) >= 4 else page_w_pt, page_w_pt))

        # Update spinboxes
        self._block_spin_signals = True
        self._header_spin.setMaximum(page_h_pt)
        self._header_spin.setValue(header_pt)

        self._footer_spin.setMaximum(page_h_pt)
        footer_from_bot = max(0.0, page_h_pt - footer_pt)
        self._footer_spin.setValue(footer_from_bot)

        self._left_spin.setMaximum(page_w_pt)
        self._left_spin.setValue(left_pt)

        self._right_spin.setMaximum(page_w_pt)
        right_from_edge = max(0.0, page_w_pt - right_pt)
        self._right_spin.setValue(right_from_edge)
        self._block_spin_signals = False

        # Build interactive graphic items
        self._build_overlay_items(page_w_px, page_h_px, header_pt, footer_pt, left_pt, right_pt, page_h_pt, page_w_pt)

    def _build_overlay_items(
        self,
        page_w_px: float,
        page_h_px: float,
        header_pt: float,
        footer_pt: float,
        left_pt: float,
        right_pt: float,
        page_h_pt: float,
        page_w_pt: float,
    ) -> None:
        """Create Header, Footer, and Side dotted lines and translucent bands."""
        h_y_px = header_pt * RENDER_DPI_SCALE
        f_y_px = footer_pt * RENDER_DPI_SCALE
        l_x_px = left_pt * RENDER_DPI_SCALE
        r_x_px = right_pt * RENDER_DPI_SCALE

        # 1. Header Exclusion Rect & Line
        self._header_rect = self._scene.addRect(
            QRectF(0, 0, page_w_px, h_y_px),
            Qt.PenStyle.NoPen,
            _CROP_OVERLAY_BRUSH,
        )
        self._header_rect.setZValue(10)

        self._header_line = self._scene.addLine(0, h_y_px, page_w_px, h_y_px, _HEADER_PEN)
        self._header_line.setZValue(12)

        self._header_badge, self._header_badge_text = self._create_badge(
            text=f"Header: {header_pt:.0f} pt  ↕ drag",
            x_px=24.0,
            y_px=max(0.0, h_y_px - 22.0),
        )

        # 2. Footer Exclusion Rect & Line
        self._footer_rect = self._scene.addRect(
            QRectF(0, f_y_px, page_w_px, page_h_px - f_y_px),
            Qt.PenStyle.NoPen,
            _CROP_OVERLAY_BRUSH,
        )
        self._footer_rect.setZValue(10)

        self._footer_line = self._scene.addLine(0, f_y_px, page_w_px, f_y_px, _FOOTER_PEN)
        self._footer_line.setZValue(12)

        footer_from_bot = max(0.0, page_h_pt - footer_pt)
        self._footer_badge, self._footer_badge_text = self._create_badge(
            text=f"Footer: {footer_from_bot:.0f} pt  ↕ drag",
            x_px=24.0,
            y_px=min(page_h_px - 22.0, f_y_px),
        )

        # 3. Left Side Exclusion Rect & Line
        self._left_rect = self._scene.addRect(
            QRectF(0, 0, l_x_px, page_h_px),
            Qt.PenStyle.NoPen,
            _CROP_OVERLAY_BRUSH,
        )
        self._left_rect.setZValue(10)

        self._left_line = self._scene.addLine(l_x_px, 0, l_x_px, page_h_px, _LEFT_PEN)
        self._left_line.setZValue(12)

        left_badge_x = max(0.0, l_x_px - 145.0) if l_x_px > 145.0 else l_x_px + 4.0
        left_badge_y = min(page_h_px - 26.0, max(28.0, page_h_px * 0.35))
        self._left_badge, self._left_badge_text = self._create_badge(
            text=f"Left: {left_pt:.0f} pt  ↔ drag",
            x_px=left_badge_x,
            y_px=left_badge_y,
            width=145.0,
        )

        # 4. Right Side Exclusion Rect & Line
        self._right_rect = self._scene.addRect(
            QRectF(r_x_px, 0, page_w_px - r_x_px, page_h_px),
            Qt.PenStyle.NoPen,
            _CROP_OVERLAY_BRUSH,
        )
        self._right_rect.setZValue(10)

        self._right_line = self._scene.addLine(r_x_px, 0, r_x_px, page_h_px, _RIGHT_PEN)
        self._right_line.setZValue(12)

        right_from_edge = max(0.0, page_w_pt - right_pt)
        right_badge_x = min(page_w_px - 145.0, r_x_px - 149.0) if r_x_px >= 149.0 else r_x_px + 4.0
        right_badge_y = min(page_h_px - 26.0, max(28.0, page_h_px * 0.55))
        self._right_badge, self._right_badge_text = self._create_badge(
            text=f"Right: {right_from_edge:.0f} pt  ↔ drag",
            x_px=right_badge_x,
            y_px=right_badge_y,
            width=145.0,
        )

        # Update view bounds for mouse dragging
        self._view.set_bounds(page_w_px, page_h_px, h_y_px, f_y_px, l_x_px, r_x_px)

    def _create_badge(
        self, text: str, x_px: float, y_px: float, width: float = 160.0
    ) -> tuple[QGraphicsRectItem, QGraphicsSimpleTextItem]:
        badge_w, badge_h = width, 22.0

        rect_item = self._scene.addRect(
            QRectF(x_px, y_px, badge_w, badge_h),
            _HANDLE_BORDER_PEN,
            _HANDLE_BG_BRUSH,
        )
        rect_item.setZValue(15)

        text_item = self._scene.addSimpleText(text)
        font = QFont("Segoe UI", 8, QFont.Weight.Bold)
        text_item.setFont(font)
        text_item.setBrush(QBrush(QColor(255, 255, 255)))
        text_item.setPos(x_px + 8, y_px + 3)
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
        page_w_pt = meta["widths_pt"][self._current_page - 1]
        page_w_px = page_w_pt * RENDER_DPI_SCALE
        page_h_px = page_h_pt * RENDER_DPI_SCALE

        page_crops = self._crop_data.setdefault(path, {})
        cur_crop = page_crops.get(self._current_page, (0.0, page_h_pt, 0.0, page_w_pt))
        cur_header_pt = cur_crop[0]
        cur_footer_pt = cur_crop[1]
        cur_left_pt = cur_crop[2] if len(cur_crop) >= 4 else 0.0
        cur_right_pt = cur_crop[3] if len(cur_crop) >= 4 else page_w_pt

        if which == "header":
            header_pt = max(0.0, min(val_pt, cur_footer_pt - 5.0))
            page_crops[self._current_page] = (header_pt, cur_footer_pt, cur_left_pt, cur_right_pt)

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
            page_crops[self._current_page] = (cur_header_pt, footer_pt, cur_left_pt, cur_right_pt)

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

        elif which == "left":
            left_pt = max(0.0, min(val_pt, cur_right_pt - 5.0))
            page_crops[self._current_page] = (cur_header_pt, cur_footer_pt, left_pt, cur_right_pt)

            l_x_px = left_pt * RENDER_DPI_SCALE
            if self._left_rect:
                self._left_rect.setRect(0, 0, l_x_px, page_h_px)
            if self._left_line:
                self._left_line.setLine(l_x_px, 0, l_x_px, page_h_px)
            if self._left_badge and self._left_badge_text:
                badge_w, badge_h = 145.0, 22.0
                bx = max(0.0, l_x_px - badge_w) if l_x_px > badge_w else l_x_px + 4.0
                by = min(page_h_px - 26.0, max(28.0, page_h_px * 0.35))
                self._left_badge.setRect(bx, by, badge_w, badge_h)
                self._left_badge_text.setPos(bx + 8, by + 3)
                self._left_badge_text.setText(f"Left: {left_pt:.0f} pt  ↔ drag")

            self._block_spin_signals = True
            self._left_spin.setValue(left_pt)
            self._block_spin_signals = False

        elif which == "right":
            right_pt = max(cur_left_pt + 5.0, min(val_pt, page_w_pt))
            page_crops[self._current_page] = (cur_header_pt, cur_footer_pt, cur_left_pt, right_pt)

            r_x_px = right_pt * RENDER_DPI_SCALE
            if self._right_rect:
                self._right_rect.setRect(r_x_px, 0, page_w_px - r_x_px, page_h_px)
            if self._right_line:
                self._right_line.setLine(r_x_px, 0, r_x_px, page_h_px)
            if self._right_badge and self._right_badge_text:
                badge_w, badge_h = 145.0, 22.0
                bx = min(page_w_px - badge_w, r_x_px - (badge_w + 4.0)) if r_x_px >= (badge_w + 4.0) else r_x_px + 4.0
                by = min(page_h_px - 26.0, max(28.0, page_h_px * 0.55))
                self._right_badge.setRect(bx, by, badge_w, badge_h)
                self._right_badge_text.setPos(bx + 8, by + 3)
                right_from_edge = max(0.0, page_w_pt - right_pt)
                self._right_badge_text.setText(f"Right: {right_from_edge:.0f} pt  ↔ drag")

            self._block_spin_signals = True
            right_from_edge = max(0.0, page_w_pt - right_pt)
            self._right_spin.setValue(right_from_edge)
            self._block_spin_signals = False

        # Update view bounds
        h_pt, f_pt, l_pt, r_pt = page_crops[self._current_page]
        self._view.set_bounds(
            page_w_px,
            page_h_px,
            h_pt * RENDER_DPI_SCALE,
            f_pt * RENDER_DPI_SCALE,
            l_pt * RENDER_DPI_SCALE,
            r_pt * RENDER_DPI_SCALE,
        )

    def _on_drag_finished(self) -> None:
        path = self._current_path()
        if not path:
            return
        meta = self._doc_meta.get(path)
        if not meta:
            return
        page_h_pt = meta["heights_pt"][self._current_page - 1]
        page_w_pt = meta["widths_pt"][self._current_page - 1]
        page_crops = self._crop_data.get(path, {})
        cur_crop = page_crops.get(self._current_page, (0.0, page_h_pt, 0.0, page_w_pt))
        h_pt = cur_crop[0]
        f_pt = cur_crop[1]
        l_pt = cur_crop[2] if len(cur_crop) >= 4 else 0.0
        r_pt = cur_crop[3] if len(cur_crop) >= 4 else page_w_pt
        f_from_bot = max(0.0, page_h_pt - f_pt)
        r_from_edge = max(0.0, page_w_pt - r_pt)
        self._tip_label.setText(
            f"Page {self._current_page}: Top <b>{h_pt:.0f} pt</b>, Bottom <b>{f_from_bot:.0f} pt</b>, "
            f"Left <b>{l_pt:.0f} pt</b>, Right <b>{r_from_edge:.0f} pt</b>. "
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

    def _on_left_spin_changed(self, val_from_left: float) -> None:
        if self._block_spin_signals:
            return
        self._on_line_dragged("left", val_from_left)

    def _on_right_spin_changed(self, val_from_right: float) -> None:
        if self._block_spin_signals:
            return
        path = self._current_path()
        meta = self._doc_meta.get(path or "")
        if not meta:
            return
        page_w_pt = meta["widths_pt"][self._current_page - 1]
        right_pt = max(0.0, page_w_pt - val_from_right)
        self._on_line_dragged("right", right_pt)

    def _clear_header(self) -> None:
        self._on_line_dragged("header", 0.0)

    def _clear_footer(self) -> None:
        path = self._current_path()
        meta = self._doc_meta.get(path or "")
        if not meta:
            return
        page_h_pt = meta["heights_pt"][self._current_page - 1]
        self._on_line_dragged("footer", page_h_pt)

    def _clear_left(self) -> None:
        self._on_line_dragged("left", 0.0)

    def _clear_right(self) -> None:
        path = self._current_path()
        meta = self._doc_meta.get(path or "")
        if not meta:
            return
        page_w_pt = meta["widths_pt"][self._current_page - 1]
        self._on_line_dragged("right", page_w_pt)

    def _clear_current_page(self) -> None:
        path = self._current_path()
        meta = self._doc_meta.get(path or "")
        if not meta:
            return
        page_h_pt = meta["heights_pt"][self._current_page - 1]
        page_w_pt = meta["widths_pt"][self._current_page - 1]
        page_crops = self._crop_data.setdefault(path, {})
        page_crops[self._current_page] = (0.0, page_h_pt, 0.0, page_w_pt)
        self._render_current_page()
        self._tip_label.setText(f"Cleared all margin crops for page {self._current_page}.")

    # ------------------------------------------------------------------
    # Apply Actions
    # ------------------------------------------------------------------

    def _apply_to_all_pages(self) -> None:
        """Apply current header, footer, and side margins to ALL pages of this document."""
        path = self._current_path()
        meta = self._doc_meta.get(path or "")
        if not meta:
            return

        page_h_pt = meta["heights_pt"][self._current_page - 1]
        page_w_pt = meta["widths_pt"][self._current_page - 1]
        page_crops = self._crop_data.setdefault(path, {})
        cur_crop = page_crops.get(self._current_page, (0.0, page_h_pt, 0.0, page_w_pt))
        cur_header_pt = cur_crop[0]
        cur_footer_pt = cur_crop[1]
        cur_left_pt = cur_crop[2] if len(cur_crop) >= 4 else 0.0
        cur_right_pt = cur_crop[3] if len(cur_crop) >= 4 else page_w_pt

        footer_margin_pt = max(0.0, page_h_pt - cur_footer_pt)
        right_margin_pt = max(0.0, page_w_pt - cur_right_pt)

        total_pages = meta["page_count"]
        for p in range(1, total_pages + 1):
            p_height = meta["heights_pt"][p - 1]
            p_width = meta["widths_pt"][p - 1]
            p_footer_pt = max(0.0, p_height - footer_margin_pt)
            p_right_pt = max(0.0, p_width - right_margin_pt)
            page_crops[p] = (cur_header_pt, p_footer_pt, cur_left_pt, p_right_pt)

        self._tip_label.setText(
            f"✅ <b>Applied to all {total_pages} pages:</b> Header {cur_header_pt:.0f} pt, "
            f"Footer {footer_margin_pt:.0f} pt, Left {cur_left_pt:.0f} pt, Right {right_margin_pt:.0f} pt."
        )

    def _apply_to_current_page(self) -> None:
        self._tip_label.setText(f"✅ Saved crops for page {self._current_page}.")

    def _clear_all_crops(self) -> None:
        path = self._current_path()
        if path and path in self._crop_data:
            self._crop_data[path] = {}
        self._render_current_page()
        self._tip_label.setText("All crops cleared for this document.")

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

    def get_crop_data(self) -> dict[str, dict[int, tuple[float, float, float, float]]]:
        """Return clean crop data for both documents."""
        result: dict[str, dict[int, tuple[float, float, float, float]]] = {}
        for path, pages in self._crop_data.items():
            meta = self._doc_meta.get(path)
            clean_pages = {}
            for p, crop in pages.items():
                p_h = meta["heights_pt"][p - 1] if meta and p <= len(meta["heights_pt"]) else 800.0
                p_w = meta["widths_pt"][p - 1] if meta and p <= len(meta["widths_pt"]) else 600.0
                h = float(crop[0])
                f = float(crop[1])
                l = float(crop[2]) if len(crop) >= 4 else 0.0
                r = float(crop[3]) if len(crop) >= 4 else p_w
                # Keep only meaningful crops
                if h > 0.0 or f < p_h or l > 0.0 or r < p_w:
                    clean_pages[p] = (h, f, l, r)
            if clean_pages:
                result[path] = clean_pages
        return result
