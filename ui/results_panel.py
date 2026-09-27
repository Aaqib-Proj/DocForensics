"""Interactive deviation list (ui/results_panel.py).

Lists every DiffItem from core/diff_engine.py as a review checklist.
Clicking (or arrowing to) a row reports its DiffItem via
``item_activated``; main.py turns that into navigation on the two
DocumentPanels.

Review workflow:
  * Every row has a category (Added, Removed, Number/dosage, Wording,
    Capitalization, Punctuation/spacing, Spelling, Moved --
    `core.diff_engine.category_of`) and a severity badge.
  * Filter chips (one per category, with counts), "Ignore minor and
    below", a text search and "Hide reviewed" narrow the list to what
    matters right now. Filters only change the VIEW -- results are
    never dropped -- and main.py shows only the listed differences as
    highlights on the documents.
  * Each difference can be Accepted (an intended change) or Rejected
    (an error to fix): buttons, the A / R / U keys, or right-click, for
    every selected row at once. "Group identical" folds identical
    differences (same category and text) into one row with a count, so
    a change repeated 400 times is one decision; right-click offers the
    same "all identical" action without grouping. Each decision records
    the reviewer and the time, for the audit trail.

The selected row is also spelled out in the comparator pane on the
right (ui/comparator_panel.py): the Original text and the Suspect text
with the exact words -- and characters inside an edited word -- that
differ marked, in the surrounding words of each document. The list says
THAT a passage differs; the comparator says what in it differs. The
splitter between them can be dragged, and the pane hidden entirely
(View > Comparator).

Visual design: colors come from ui/theme.py (light or dark). Severity is
a tinted label cell (colored text on a faint tint of the same color);
reviewed rows are dimmed; long text is truncated with the full text in
a tooltip.
"""

from __future__ import annotations

import getpass
from dataclasses import dataclass
from datetime import datetime, timezone

from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.diff_engine import CATEGORY_LABELS, CATEGORY_ORDER, DiffItem, category_of
from core.token_map import TokenMap
from ui import theme
from ui.comparator_panel import ComparatorPanel

_SEVERITY_LABEL = {"reorder": "Moved"}

ACCEPTED = "accepted"
REJECTED = "rejected"
_STATUS_LABEL = {ACCEPTED: "\u2713 Accepted", REJECTED: "\u2717 Rejected"}

_COLUMN_LABELS = ["Status", "Severity", "Category", "Count", "Approved Text", "Suspect Text"]
_COL_STATUS, _COL_SEVERITY, _COL_CATEGORY, _COL_COUNT, _COL_APPROVED, _COL_SUSPECT = range(6)

# Long text (e.g. a large deleted passage) is truncated in the cell to
# keep rows readable; the full text is still available via tooltip.
_MAX_CELL_CHARS = 320

_MINOR_AND_BELOW = ("minor", "typo", "reorder")


@dataclass
class Decision:
    status: str  # ACCEPTED or REJECTED
    reviewer: str
    decided_at: datetime  # UTC


def _group_key(item: DiffItem) -> tuple[str, str, str]:
    return (category_of(item), item.approved_text, item.suspect_text)


class ResultsPanel(QWidget):
    """Interactive deviation list: filters, grouping and accept/reject.

    Signals:
        item_activated: Emitted with the DiffItem for the row the user
            clicked or moved to with the keyboard. Not emitted for
            `select_item` (a selection made because the difference was
            clicked in a viewer).
        view_changed: The set of listed (unfiltered) differences or
            their review status changed -- main.py redraws the document
            highlights from `listed_items()`.
    """

    item_activated = Signal(object)  # DiffItem
    view_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[DiffItem] = []
        self._categories: dict[int, str] = {}  # id(item) -> category
        self._decisions: dict[int, Decision] = {}  # id(item) -> decision
        self._viewed_items: set[int] = set()  # id(item) -> marked as viewed
        # Row `i` shows the group self._row_groups[i]; self._visible_items[i]
        # is its first member (the one a click navigates to).
        self._row_groups: list[list[DiffItem]] = []
        self._visible_items: list[DiffItem] = []
        # True while the table's selection is changed by code, so it
        # doesn't count as the user picking a row.
        self._programmatic = False
        self.reviewer = getpass.getuser()

        # --- category chips ---
        self._chips: dict[str, QToolButton] = {}
        chip_layout = QHBoxLayout()
        chip_layout.setContentsMargins(0, 0, 0, 0)
        chip_layout.setSpacing(6)
        show_label = QLabel("Show")
        show_label.setObjectName("muted")
        chip_layout.addWidget(show_label)
        for category in CATEGORY_ORDER:
            chip = QToolButton()
            chip.setCheckable(True)
            chip.setChecked(True)
            chip.setProperty("chip", True)
            chip.setToolTip(f"Show or hide '{CATEGORY_LABELS[category]}' differences")
            chip.toggled.connect(self._refresh_table)
            self._chips[category] = chip
            chip_layout.addWidget(chip)
        chip_layout.addStretch()
        self._update_chip_labels()

        # --- view options ---
        self.ignore_minor_checkbox = QCheckBox("Ignore minor and below")
        self.ignore_minor_checkbox.setToolTip(
            "Hides (does not delete) everything at or below 'minor': "
            "punctuation-only changes, character-level typos, and moved "
            "text -- leaving only critical and major differences."
        )
        self.ignore_minor_checkbox.stateChanged.connect(self._refresh_table)
        self.group_checkbox = QCheckBox("Group identical")
        self.group_checkbox.setToolTip(
            "Show identical differences (same category and text) as one row "
            "with a count, so one Accept/Reject decides all of them."
        )
        self.group_checkbox.stateChanged.connect(self._refresh_table)
        self.hide_reviewed_checkbox = QCheckBox("Hide reviewed")
        self.hide_reviewed_checkbox.setToolTip("Hide differences already accepted or rejected.")
        self.hide_reviewed_checkbox.stateChanged.connect(self._refresh_table)

        self.hide_viewed_checkbox = QCheckBox("Hide viewed")
        self.hide_viewed_checkbox.setToolTip("Hide differences already viewed, focusing on unseen ones.")
        self.hide_viewed_checkbox.stateChanged.connect(self._refresh_table)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Filter by text…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setMaximumWidth(200)
        self.search_box.textChanged.connect(self._refresh_table)

        self.unseen_badge = QLabel()
        self.unseen_badge.setObjectName("unseenBadge")
        self.summary_label = QLabel()
        self.summary_label.setObjectName("muted")

        self.unview_button = QPushButton("👁 Unview")
        self.unview_button.setToolTip("Mark the selected difference(s) as unviewed")
        self.unview_button.clicked.connect(self.unview_selected)

        self.accept_button = QPushButton("\u2713 Accept")
        self.accept_button.setToolTip("Mark the selected differences as intended changes (A)")
        self.accept_button.clicked.connect(lambda: self.decide_selected(ACCEPTED))
        self.reject_button = QPushButton("\u2717 Reject")
        self.reject_button.setToolTip("Mark the selected differences as errors to fix (R)")
        self.reject_button.clicked.connect(lambda: self.decide_selected(REJECTED))
        self.reset_button = QPushButton("Reset")
        self.reset_button.setToolTip("Clear the review decision of the selected differences (U)")
        self.reset_button.clicked.connect(lambda: self.decide_selected(None))

        options_layout = QHBoxLayout()
        options_layout.setContentsMargins(0, 0, 0, 0)
        options_layout.setSpacing(10)
        options_layout.addWidget(self.ignore_minor_checkbox)
        options_layout.addWidget(self.group_checkbox)
        options_layout.addWidget(self.hide_reviewed_checkbox)
        options_layout.addWidget(self.hide_viewed_checkbox)
        options_layout.addWidget(self.search_box)
        options_layout.addWidget(self.unseen_badge)
        options_layout.addWidget(self.summary_label)
        options_layout.addStretch()
        options_layout.addWidget(self.unview_button)
        options_layout.addWidget(self.accept_button)
        options_layout.addWidget(self.reject_button)
        options_layout.addWidget(self.reset_button)

        # --- table ---
        self.table = QTableWidget(0, len(_COLUMN_LABELS))
        self.table.setHorizontalHeaderLabels(_COLUMN_LABELS)
        header = self.table.horizontalHeader()
        # Fixed widths: "resize to contents" re-measures every row on each
        # cell update, which made filling a few hundred rows take seconds.
        for column, width in ((_COL_STATUS, 104), (_COL_SEVERITY, 84), (_COL_CATEGORY, 150), (_COL_COUNT, 60)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(column, width)
        header.setSectionResizeMode(_COL_APPROVED, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COL_SUSPECT, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(70)
        header.setHighlightSections(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # One line per difference; the full text is in the tooltip.
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setShowGrid(True)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.currentCellChanged.connect(self._on_current_cell_changed)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        for key, status in (("A", ACCEPTED), ("R", REJECTED), ("U", None)):
            shortcut = QShortcut(QKeySequence(key), self.table)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(lambda s=status: self.decide_selected(s))

        # --- comparator (what changed inside the selected row) ---
        self.comparator = ComparatorPanel()
        self.comparator.occurrence_selected.connect(self.item_activated.emit)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("deviationSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.comparator)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([760, 420])

        layout = QVBoxLayout()
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        layout.addLayout(chip_layout)
        layout.addLayout(options_layout)
        layout.addWidget(self.splitter)
        self.setLayout(layout)
        self.table.itemSelectionChanged.connect(self._update_comparator)
        self._update_action_buttons()
        theme.manager().changed.connect(lambda: self._refresh_table(keep_selection=True))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_token_map(self, token_map: TokenMap | None) -> None:
        """Hand the comparison's word alignment to the comparator, which
        reads each difference's surrounding words from it."""
        self.comparator.set_token_map(token_map)

    def set_comparator_visible(self, visible: bool) -> None:
        self.comparator.setVisible(visible)
        if visible:
            self._update_comparator()

    def comparator_visible(self) -> bool:
        return self.comparator.isVisible()

    def set_results(self, diff_items: list[DiffItem]) -> None:
        """Replace the list with a fresh set of results (decisions reset)."""
        self._items = diff_items
        self._categories = {id(item): category_of(item) for item in diff_items}
        self._decisions = {}
        self._viewed_items = set()
        self._update_chip_labels()
        self._refresh_table()

    def clear_results(self) -> None:
        self.set_results([])

    def rowCount(self) -> int:  # noqa: N802 -- QTableWidget-style name, for callers/tests
        return self.table.rowCount()

    def listed_items(self) -> list[DiffItem]:
        """Every difference currently passing the filters (all members of
        grouped rows)."""
        return [item for group in self._row_groups for item in group]

    def decision(self, item: DiffItem) -> Decision | None:
        return self._decisions.get(id(item))

    def category(self, item: DiffItem) -> str:
        return self._categories.get(id(item)) or category_of(item)

    def all_items(self) -> list[DiffItem]:
        return list(self._items)

    def select_item(self, item: DiffItem) -> bool:
        """Select and scroll to ``item``'s row without emitting
        ``item_activated``. Returns False if the row is hidden by a
        filter (or the item isn't in the results)."""
        for row, group in enumerate(self._row_groups):
            if any(member is item for member in group):
                self._programmatic = True
                try:
                    self.table.setCurrentCell(row, 0)
                    self.table.selectRow(row)
                finally:
                    self._programmatic = False
                self.table.scrollToItem(
                    self.table.item(row, 0), QAbstractItemView.ScrollHint.PositionAtCenter
                )
                self._update_action_buttons()
                self._update_comparator()
                return True
        self._programmatic = True
        try:
            self.table.clearSelection()
        finally:
            self._programmatic = False
        self._update_action_buttons()
        self._update_comparator()
        return False

    def select_adjacent(self, step: int) -> bool:
        """Move to the next (``step`` = 1) or previous (-1) listed
        difference and activate it. Returns False at either end."""
        if not self._row_groups:
            return False
        row = self.table.currentRow()
        target = (0 if step > 0 else len(self._row_groups) - 1) if row < 0 else row + step
        if not 0 <= target < len(self._row_groups):
            return False
        self._programmatic = True
        try:
            self.table.clearSelection()
            self.table.setCurrentCell(target, 0)
            self.table.selectRow(target)
        finally:
            self._programmatic = False
        self.table.scrollToItem(self.table.item(target, 0))
        self._update_action_buttons()
        self._update_comparator()
        self._activate(target)
        return True

    def is_viewed(self, item: DiffItem) -> bool:
        """True if ``item`` has been viewed by the reviewer."""
        return id(item) in self._viewed_items

    def unview_selected(self) -> None:
        """Mark the selected deviation(s) as unviewed."""
        selected = self._selected_items()
        if not selected:
            row = self.table.currentRow()
            if 0 <= row < len(self._row_groups):
                selected = self._row_groups[row]
        if not selected:
            return
        for item in selected:
            self._viewed_items.discard(id(item))
        self._refresh_table(keep_selection=True)

    def view_selected(self) -> None:
        """Mark the selected deviation(s) as viewed."""
        selected = self._selected_items()
        if not selected:
            row = self.table.currentRow()
            if 0 <= row < len(self._row_groups):
                selected = self._row_groups[row]
        if not selected:
            return
        for item in selected:
            self._viewed_items.add(id(item))
        self._refresh_table(keep_selection=True)

    def decide(self, items: list[DiffItem], status: str | None) -> None:
        """Accept (``ACCEPTED``), reject (``REJECTED``) or reset (None)
        ``items``, recorded for the current reviewer and time."""
        now = datetime.now(timezone.utc)
        for item in items:
            self._viewed_items.add(id(item))
            if status is None:
                self._decisions.pop(id(item), None)
            else:
                self._decisions[id(item)] = Decision(status, self.reviewer, now)
        self._refresh_table(keep_selection=True)

    def decide_selected(self, status: str | None) -> None:
        self.decide(self._selected_items(), status)

    def identical_to(self, item: DiffItem) -> list[DiffItem]:
        key = _group_key(item)
        return [other for other in self._items if _group_key(other) == key]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _update_chip_labels(self) -> None:
        counts = {category: 0 for category in CATEGORY_ORDER}
        for item in self._items:
            counts[self._categories[id(item)]] += 1
        for category, chip in self._chips.items():
            chip.setText(f"{CATEGORY_LABELS[category]} {counts[category]}")
            chip.setEnabled(counts[category] > 0)

    def _passes_filters(self, item: DiffItem) -> bool:
        if not self._chips[self._categories[id(item)]].isChecked():
            return False
        if self.ignore_minor_checkbox.isChecked() and item.severity in _MINOR_AND_BELOW:
            return False
        if self.hide_reviewed_checkbox.isChecked() and id(item) in self._decisions:
            return False
        if self.hide_viewed_checkbox.isChecked() and id(item) in self._viewed_items:
            return False
        needle = self.search_box.text().strip().casefold()
        if needle and needle not in item.approved_text.casefold() and needle not in item.suspect_text.casefold():
            return False
        return True

    def _selected_items(self) -> list[DiffItem]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        return [item for row in rows if row < len(self._row_groups) for item in self._row_groups[row]]

    def _refresh_table(self, *_args, keep_selection: bool = False) -> None:
        selected = {id(item) for item in self._selected_items()} if keep_selection else set()
        current = self.table.currentRow()
        current_item = (
            self._visible_items[current] if keep_selection and 0 <= current < len(self._visible_items) else None
        )

        listed = [item for item in self._items if self._passes_filters(item)]
        if self.group_checkbox.isChecked():
            groups: dict[tuple, list[DiffItem]] = {}
            for item in listed:
                groups.setdefault(_group_key(item), []).append(item)
            self._row_groups = list(groups.values())
        else:
            self._row_groups = [[item] for item in listed]
        self._visible_items = [group[0] for group in self._row_groups]

        self._programmatic = True
        self.table.setUpdatesEnabled(False)
        try:
            self.table.clearSelection()
            self.table.clearSpans()
            self.table.setRowCount(len(self._row_groups))
            for row, group in enumerate(self._row_groups):
                self._fill_row(row, group)
            if keep_selection:
                for row, group in enumerate(self._row_groups):
                    if any(id(member) in selected for member in group):
                        self.table.selectRow(row)
                    if current_item is not None and any(m is current_item for m in group):
                        self.table.setCurrentCell(row, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            self.table.setUpdatesEnabled(True)
            self._programmatic = False

        self._update_summary_labels(len(listed))
        self._update_action_buttons()
        self._update_comparator()
        self.view_changed.emit()

    def _update_summary_labels(self, listed_count: int | None = None) -> None:
        t = theme.tokens()
        total_items = len(self._items)
        unseen_count = sum(1 for i in self._items if id(i) not in self._viewed_items)
        unseen_pct = (unseen_count / total_items * 100) if total_items > 0 else 0
        reviewed = [self._decisions[id(i)].status for i in self._items if id(i) in self._decisions]
        count_shown = listed_count if listed_count is not None else len(self._visible_items)

        self.summary_label.setText(
            f"{count_shown} of {total_items} shown · "
            f"{reviewed.count(ACCEPTED)} accepted · {reviewed.count(REJECTED)} rejected · "
            f"{total_items - len(reviewed)} to review"
        )

        if total_items == 0:
            self.unseen_badge.setVisible(False)
        else:
            self.unseen_badge.setVisible(True)
            if unseen_count == 0:
                badge_bg = "#064e3b" if t.dark else "#dcfce7"
                badge_fg = "#34d399" if t.dark else "#15803d"
                badge_border = "#059669" if t.dark else "#86efac"
                self.unseen_badge.setText("✓ All viewed")
            else:
                badge_bg = "#1e3a8a" if t.dark else "#eff6ff"
                badge_fg = "#60a5fa" if t.dark else "#1d4ed8"
                badge_border = "#3b82f6" if t.dark else "#bfdbfe"
                self.unseen_badge.setText(f"● {unseen_pct:.0f}% unseen ({unseen_count}/{total_items})")
            self.unseen_badge.setStyleSheet(
                f"background: {badge_bg}; color: {badge_fg}; border: 1px solid {badge_border}; "
                f"border-radius: 11px; padding: 2px 10px; font-weight: 600; font-size: 11px;"
            )

    def _group_status(self, group: list[DiffItem]) -> str | None:
        statuses = {self._decisions[id(m)].status if id(m) in self._decisions else None for m in group}
        if len(statuses) == 1:
            return statuses.pop()
        return "mixed"

    def _fill_row(self, row: int, group: list[DiffItem]) -> None:
        t = theme.tokens()
        item = group[0]
        status = self._group_status(group)
        text_color = QColor(t.muted if status in (ACCEPTED, REJECTED) else t.text)

        if status == "mixed":
            done = sum(1 for m in group if id(m) in self._decisions)
            status_cell = self._make_text_cell(f"{done}/{len(group)} reviewed", QColor(t.muted))
        elif status is None:
            is_viewed = all(id(m) in self._viewed_items for m in group)
            if is_viewed:
                status_cell = self._make_text_cell("Viewed", QColor(t.muted), italic=True)
            else:
                dot_color = QColor("#3b82f6" if not t.dark else "#60a5fa")
                status_cell = self._make_text_cell("● Unseen", dot_color, bold=True)
        else:
            decision = self._decisions[id(item)]
            color = QColor(t.success if status == ACCEPTED else t.danger)
            status_cell = self._make_text_cell(_STATUS_LABEL[status], color, bold=True)
            status_cell.setToolTip(
                f"{_STATUS_LABEL[status][2:]} by {decision.reviewer} on "
                f"{decision.decided_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}"
            )
        self.table.setItem(row, _COL_STATUS, status_cell)
        self.table.setItem(row, _COL_SEVERITY, self._make_severity_cell(item.severity))
        self.table.setItem(
            row, _COL_CATEGORY,
            self._make_text_cell(CATEGORY_LABELS[self._categories[id(item)]], text_color),
        )
        count_cell = self._make_text_cell(
            f"\u00d7{len(group)}" if len(group) > 1 else "", text_color, bold=True, placeholder=""
        )
        count_cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, _COL_COUNT, count_cell)
        if item.kind == "added_in_suspect":
            self.table.setSpan(row, _COL_APPROVED, 1, 2)
            self.table.setItem(row, _COL_APPROVED, self._make_text_cell(f"Inserted: {item.suspect_text}", text_color))
        elif item.kind == "removed_in_suspect":
            self.table.setSpan(row, _COL_APPROVED, 1, 2)
            self.table.setItem(row, _COL_APPROVED, self._make_text_cell(f"Deleted on suspect: {item.approved_text}", text_color))
        elif item.kind in ("reordered", "reordered_section"):
            self.table.setSpan(row, _COL_APPROVED, 1, 2)
            self.table.setItem(row, _COL_APPROVED, self._make_text_cell(f"Moved: {item.approved_text or item.suspect_text}", text_color))
        else:
            self.table.setSpan(row, _COL_APPROVED, 1, 1)
            self.table.setItem(row, _COL_APPROVED, self._make_text_cell(item.approved_text, text_color))
            self.table.setItem(row, _COL_SUSPECT, self._make_text_cell(item.suspect_text, text_color))

    def _make_severity_cell(self, severity: str) -> QTableWidgetItem:
        fg, bg = theme.tokens().severity(severity)
        cell = QTableWidgetItem(_SEVERITY_LABEL.get(severity, severity.capitalize()))
        cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        cell.setBackground(QColor(bg))
        cell.setForeground(QColor(fg))
        font = QFont()
        font.setBold(True)
        cell.setFont(font)
        return cell

    def _make_text_cell(
        self, text: str, color: QColor, bold: bool = False, italic: bool = False, placeholder: str = "\u2014"
    ) -> QTableWidgetItem:
        display_text = text or placeholder
        truncated = len(display_text) > _MAX_CELL_CHARS
        shown = display_text[: _MAX_CELL_CHARS - 1] + "\u2026" if truncated else display_text
        cell = QTableWidgetItem(shown)
        cell.setForeground(color if text else QColor(theme.tokens().muted))
        if bold or italic:
            font = QFont()
            if bold:
                font.setBold(True)
            if italic:
                font.setItalic(True)
            cell.setFont(font)
        if truncated or len(text) > 40:
            # The full, untruncated text is always available on hover.
            cell.setToolTip(display_text)
        return cell

    def _update_comparator(self) -> None:
        """Show the current row's difference in the comparator (or clear
        it when nothing is selected)."""
        if self._programmatic or not self.comparator.isVisible():
            return  # mid-rebuild; the caller updates once it settles
        row = self.table.currentRow()
        selected = {index.row() for index in self.table.selectedIndexes()}
        if row not in selected or not 0 <= row < len(self._row_groups):
            row = min(selected) if selected else -1
        if not 0 <= row < len(self._row_groups):
            self.comparator.clear()
            return
        group = self._row_groups[row]
        self.comparator.show_item(group[0], count=len(group), status=self._decision_text(group), all_items=group)

    def _decision_text(self, group: list[DiffItem]) -> str:
        """The review decision of a row, in words, for the comparator."""
        status = self._group_status(group)
        if status is None:
            return ""
        if status == "mixed":
            done = sum(1 for member in group if id(member) in self._decisions)
            return f"{done} of {len(group)} reviewed."
        decision = self._decisions[id(group[0])]
        when = decision.decided_at.astimezone().strftime("%Y-%m-%d %H:%M")
        return f"{_STATUS_LABEL[status][2:]} by {decision.reviewer} on {when}."

    def _update_action_buttons(self) -> None:
        has_selection = bool(self.table.selectedIndexes())
        for button in (self.accept_button, self.reject_button, self.reset_button):
            button.setEnabled(has_selection)

    def _show_context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        if not any(index.row() == row for index in self.table.selectedIndexes()):
            self.table.selectRow(row)
        menu = QMenu(self)
        for label, status in (("Accept", ACCEPTED), ("Reject", REJECTED), ("Reset", None)):
            action = QAction(label, menu)
            action.triggered.connect(lambda _=False, s=status: self.decide_selected(s))
            menu.addAction(action)
        menu.addSeparator()
        unview_act = QAction("Mark as Unviewed", menu)
        unview_act.triggered.connect(self.unview_selected)
        menu.addAction(unview_act)
        view_act = QAction("Mark as Viewed", menu)
        view_act.triggered.connect(self.view_selected)
        menu.addAction(view_act)
        identical = self.identical_to(self._visible_items[row])
        if len(identical) > 1:
            menu.addSeparator()
            for label, status in (("Accept", ACCEPTED), ("Reject", REJECTED)):
                action = QAction(f"{label} all {len(identical)} identical", menu)
                action.triggered.connect(lambda _=False, s=status, items=identical: self.decide(items, s))
                menu.addAction(action)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _on_current_cell_changed(self, row: int, _col: int, previous_row: int, _prev_col: int) -> None:
        self._update_action_buttons()
        if self._programmatic or row == previous_row:
            return
        if QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
            return  # a mouse click: cellClicked activates the row
        self._activate(row)  # keyboard (arrow keys, Page Up/Down, ...)

    def _on_cell_clicked(self, row: int, _column: int) -> None:
        self._update_action_buttons()
        if QApplication.keyboardModifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        ):
            return  # extending a multi-selection, not navigating
        self._activate(row)

    def _activate(self, row: int) -> None:
        if 0 <= row < len(self._visible_items):
            group = self._row_groups[row]
            newly_viewed = False
            for m in group:
                if id(m) not in self._viewed_items:
                    self._viewed_items.add(id(m))
                    newly_viewed = True
            if newly_viewed:
                self._fill_row(row, group)
                self._update_summary_labels()
                self._update_action_buttons()
            self.item_activated.emit(self._visible_items[row])
