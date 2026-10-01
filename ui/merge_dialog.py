"""Merge Documents dialog (ui/merge_dialog.py).

A dedicated dialog for adding, reordering, and combining multiple split
documents (e.g., suspect PDF part 1 + part 2) into a single continuous
document ready for comparison.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.pdf_merge import (
    SUPPORTED_MERGE_EXTENSIONS,
    inspect_document,
    merge_pdf_documents,
    natural_sort_key,
)
from ui import theme

logger = logging.getLogger(__name__)

_MERGE_FILE_FILTER = (
    "Supported Documents (*.pdf *.docx);;"
    "PDF Files (*.pdf);;Word Documents (*.docx)"
)


class MergeDocumentsDialog(QDialog):
    """Dialog for organizing and merging multiple files into one document."""

    def __init__(
        self,
        parent: QWidget | None = None,
        initial_files: list[str] | None = None,
        title: str = "Suspect",
        can_compare: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Merge Documents — {title}")
        self.resize(750, 480)
        self.setMinimumSize(600, 360)
        self.setAcceptDrops(True)

        self._title = title
        self._can_compare = can_compare
        self._files: list[str] = []
        self._custom_save_path: str | None = None

        # Output properties after successful merge
        self.merged_path: str | None = None
        self.source_files: list[str] = []
        self.file_infos: list[dict[str, Any]] = []
        self.should_compare: bool = False

        self._build_ui()
        self._apply_theme()

        if initial_files:
            self.add_files(initial_files)

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(18, 16, 18, 16)
        root_layout.setSpacing(12)

        # --- Header ---
        header_widget = QWidget()
        header_layout = QVBoxLayout(header_widget)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(3)

        self.heading_label = QLabel(f"Merge Split Files for {self._title}")
        heading_font = QFont()
        heading_font.setPointSize(12)
        heading_font.setBold(True)
        self.heading_label.setFont(heading_font)

        self.subheading_label = QLabel(
            "Add and arrange files in reading order. They will be combined into a single "
            "continuous document for side-by-side comparison."
        )
        self.subheading_label.setObjectName("muted")
        self.subheading_label.setWordWrap(True)

        header_layout.addWidget(self.heading_label)
        header_layout.addWidget(self.subheading_label)
        root_layout.addWidget(header_widget)

        # --- Middle: Table + Action Buttons ---
        middle_layout = QHBoxLayout()
        middle_layout.setSpacing(10)

        # Files Table
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["#", "File Name", "Pages", "Merged Range", "Size"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.table.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.table.setDropIndicatorShown(True)
        self.table.itemSelectionChanged.connect(self._update_button_states)

        # Column sizing
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

        middle_layout.addWidget(self.table, stretch=1)

        # Right-side button column
        btn_column = QVBoxLayout()
        btn_column.setSpacing(6)

        self.btn_add = QPushButton("Add Files…")
        self.btn_add.setToolTip("Select one or more files to add to the merge list")
        self.btn_add.clicked.connect(self.on_add_files_clicked)

        self.btn_up = QPushButton("Move Up")
        self.btn_up.setToolTip("Move selected file earlier in reading order")
        self.btn_up.clicked.connect(self.on_move_up_clicked)

        self.btn_down = QPushButton("Move Down")
        self.btn_down.setToolTip("Move selected file later in reading order")
        self.btn_down.clicked.connect(self.on_move_down_clicked)

        self.btn_remove = QPushButton("Remove")
        self.btn_remove.setToolTip("Remove selected file from the merge list")
        self.btn_remove.clicked.connect(self.on_remove_clicked)

        self.btn_clear = QPushButton("Clear All")
        self.btn_clear.clicked.connect(self.on_clear_clicked)

        btn_column.addWidget(self.btn_add)
        btn_column.addWidget(self.btn_up)
        btn_column.addWidget(self.btn_down)
        btn_column.addWidget(self.btn_remove)
        btn_column.addWidget(self.btn_clear)
        btn_column.addStretch(1)

        middle_layout.addLayout(btn_column)
        root_layout.addLayout(middle_layout, stretch=1)

        # --- Divider line ---
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        root_layout.addWidget(sep)

        # --- Bottom bar: Summary, Options, Dialog Buttons ---
        bottom_layout = QHBoxLayout()
        bottom_layout.setSpacing(12)

        # Left: Summary info
        self.summary_label = QLabel("No files selected")
        self.summary_label.setObjectName("muted")
        bottom_layout.addWidget(self.summary_label)

        bottom_layout.addStretch(1)

        # Compare immediately checkbox
        self.compare_checkbox = QCheckBox("Compare immediately after merging")
        self.compare_checkbox.setChecked(self._can_compare)
        self.compare_checkbox.setToolTip("Automatically run comparison once the merged document loads")
        bottom_layout.addWidget(self.compare_checkbox)

        # Save copy link / button
        self.btn_save_copy = QPushButton("Save Copy As…")
        self.btn_save_copy.setToolTip("Optionally choose where to save a permanent copy of the merged file")
        self.btn_save_copy.clicked.connect(self.on_save_copy_clicked)
        bottom_layout.addWidget(self.btn_save_copy)

        # Cancel button
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        bottom_layout.addWidget(self.btn_cancel)

        # Primary Merge Button
        primary_text = "Merge & Compare" if self._can_compare else "Merge & Load"
        self.btn_merge = QPushButton(primary_text)
        self.btn_merge.setObjectName("primary")
        self.btn_merge.setDefault(True)
        self.btn_merge.clicked.connect(self.on_merge_clicked)
        bottom_layout.addWidget(self.btn_merge)

        root_layout.addLayout(bottom_layout)

        self._update_button_states()

    def _apply_theme(self) -> None:
        t = theme.tokens()
        self.heading_label.setStyleSheet(f"color: {t.text};")
        self.subheading_label.setStyleSheet(f"color: {t.muted};")
        self.summary_label.setStyleSheet(f"color: {t.text}; font-weight: 500;")

    # ------------------------------------------------------------------
    # Drag and drop support
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    ext = os.path.splitext(url.toLocalFile())[1].lower()
                    if ext in SUPPORTED_MERGE_EXTENSIONS:
                        event.acceptProposedAction()
                        return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = []
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    p = url.toLocalFile()
                    if os.path.splitext(p)[1].lower() in SUPPORTED_MERGE_EXTENSIONS:
                        paths.append(p)
        if paths:
            event.acceptProposedAction()
            self.add_files(paths)

    # ------------------------------------------------------------------
    # File list management
    # ------------------------------------------------------------------

    def add_files(self, paths: list[str]) -> None:
        """Add new files to the list, filtering duplicates."""
        existing = {os.path.normcase(os.path.abspath(p)) for p in self._files}
        new_valid = []
        for p in paths:
            normalized = os.path.normcase(os.path.abspath(p))
            if normalized not in existing:
                new_valid.append(p)
                existing.add(normalized)

        if not new_valid:
            return

        # Sort incoming batch naturally so part1, part2, part10 are in order
        new_valid.sort(key=lambda x: natural_sort_key(os.path.basename(x)))
        self._files.extend(new_valid)
        self._refresh_table()

    def on_add_files_clicked(self) -> None:
        from ui.document_panel import DocumentPanel
        start_dir = DocumentPanel.last_dir or ""
        paths, _ = QFileDialog.getOpenFileNames(
            self, f"Select Files to Merge for {self._title}", start_dir, _MERGE_FILE_FILTER
        )
        if paths:
            DocumentPanel.last_dir = os.path.dirname(paths[0])
            self.add_files(paths)

    def on_move_up_clicked(self) -> None:
        row = self.table.currentRow()
        if row > 0:
            self._files[row - 1], self._files[row] = self._files[row], self._files[row - 1]
            self._refresh_table()
            self.table.selectRow(row - 1)

    def on_move_down_clicked(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._files) - 1:
            self._files[row], self._files[row + 1] = self._files[row + 1], self._files[row]
            self._refresh_table()
            self.table.selectRow(row + 1)

    def on_remove_clicked(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._files):
            self._files.pop(row)
            self._refresh_table()
            new_row = min(row, len(self._files) - 1)
            if new_row >= 0:
                self.table.selectRow(new_row)

    def on_clear_clicked(self) -> None:
        if self._files:
            self._files.clear()
            self._refresh_table()

    def on_save_copy_clicked(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Merged PDF As", "merged_document.pdf", "PDF Files (*.pdf)"
        )
        if path:
            self._custom_save_path = path
            self.btn_save_copy.setText(f"Save: {os.path.basename(path)}")

    def _refresh_table(self) -> None:
        self.table.setRowCount(len(self._files))
        total_pages = 0
        current_page = 1
        has_error = False

        for row, path in enumerate(self._files):
            info = inspect_document(path)

            # Column 0: Index
            item_num = QTableWidgetItem(f"#{row + 1}")
            item_num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_num.setFlags(item_num.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, item_num)

            # Column 1: Name + Tooltip
            item_name = QTableWidgetItem(info["name"])
            item_name.setToolTip(f"{path}\n{info.get('error') or ''}")
            item_name.setFlags(item_name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if not info["is_valid"]:
                item_name.setForeground(QColor(theme.tokens().danger))
                has_error = True
            self.table.setItem(row, 1, item_name)

            # Column 2: Pages
            pages = info["page_count"]
            item_pages = QTableWidgetItem(f"{pages} page{'s' if pages != 1 else ''}" if info["is_valid"] else "Invalid")
            item_pages.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_pages.setFlags(item_pages.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 2, item_pages)

            # Column 3: Merged Range
            if info["is_valid"] and pages > 0:
                start_p = current_page
                end_p = current_page + pages - 1
                range_str = f"p. {start_p}" if start_p == end_p else f"p. {start_p} \u2013 {end_p}"
                current_page += pages
                total_pages += pages
            else:
                range_str = "\u2014"
            item_range = QTableWidgetItem(range_str)
            item_range.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_range.setFlags(item_range.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 3, item_range)

            # Column 4: Size
            item_size = QTableWidgetItem(info["size_formatted"])
            item_size.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item_size.setFlags(item_size.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 4, item_size)

        count = len(self._files)
        if count == 0:
            self.summary_label.setText("No files selected. Add split documents to merge.")
        elif count == 1:
            self.summary_label.setText(f"1 file selected ({total_pages} page{'s' if total_pages != 1 else ''}) \u00b7 Add at least one more file to merge.")
        else:
            self.summary_label.setText(f"Total: {count} files \u00b7 {total_pages} continuous pages")

        self._has_error = has_error
        self._total_pages = total_pages
        self._update_button_states()

    def _update_button_states(self) -> None:
        row = self.table.currentRow()
        count = len(self._files)

        self.btn_up.setEnabled(row > 0)
        self.btn_down.setEnabled(0 <= row < count - 1)
        self.btn_remove.setEnabled(row >= 0)
        self.btn_clear.setEnabled(count > 0)

        # Merge button requires at least 2 files and no invalid files
        can_merge = count >= 2 and not getattr(self, "_has_error", False)
        self.btn_merge.setEnabled(can_merge)

        # Update primary button label based on compare checkbox
        if self.compare_checkbox.isChecked():
            self.btn_merge.setText("Merge & Compare")
        else:
            self.btn_merge.setText("Merge & Load")

    # ------------------------------------------------------------------
    # Merge execution
    # ------------------------------------------------------------------

    def on_merge_clicked(self) -> None:
        if len(self._files) < 2:
            QMessageBox.warning(
                self,
                "Need At Least 2 Files",
                "Please add at least 2 files to merge into a single document."
            )
            return

        self.btn_merge.setEnabled(False)
        self.btn_merge.setText("Merging…")
        self.repaint()

        try:
            merged_path, file_infos = merge_pdf_documents(
                self._files,
                output_path=self._custom_save_path,
                prefix=f"merged_{self._title.lower()}",
            )
            self.merged_path = merged_path
            self.source_files = list(self._files)
            self.file_infos = file_infos
            self.should_compare = self.compare_checkbox.isChecked()
            self.accept()
        except Exception as exc:
            logger.exception("Failed to merge files")
            QMessageBox.critical(self, "Merge Failed", f"Could not merge the selected files:\n\n{exc}")
            self.btn_merge.setEnabled(True)
            self._update_button_states()
