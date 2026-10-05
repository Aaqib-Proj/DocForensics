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

from PySide6.QtCore import QItemSelectionModel, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
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
from core.review_store import fingerprints
from core.token_map import TokenMap
from ui import theme
from ui.comparator_panel import ComparatorPanel
from ui.flow_layout import FlowLayout, FlowRow

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

# "Moved" is text both documents contain, in a different place. It is
# shown as a category (with its count) but not listed by default --
# see the chip tooltip below.
_MOVED_CATEGORY = "moved"
_MOVED_CHIP_TOOLTIP = (
    "Text that BOTH documents contain, in a different place -- a column that "
    "reflowed, a block printed in a different order, a section relocated. "
    "Any wording that actually changed inside a moved passage is reported as "
    "its own row, at its own severity."
)


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
    # A decision, a "viewed" mark or a rematch changed: the review worth
    # keeping on disk (see core/review_store.py) is different now.
    review_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[DiffItem] = []
        self._categories: dict[int, str] = {}  # id(item) -> category
        self._decisions: dict[int, Decision] = {}  # id(item) -> decision
        # (position, original item, rows that replaced it) per rematch,
        # newest last -- `undo_rematch` walks it back.
        self._rematches: list[tuple[int, DiffItem, list[DiffItem]]] = []
        # Coalesces comparator redraws while the selection is moving.
        self._comparator_timer = QTimer(self)
        self._comparator_timer.setSingleShot(True)
        self._comparator_timer.setInterval(30)
        self._comparator_timer.timeout.connect(lambda: self._update_comparator(immediate=True))
        self._viewed_items: set[int] = set()  # id(item) -> marked as viewed
        # Every rematch and undo, oldest first, for the report: a row a
        # rematch removed is otherwise gone without a trace.
        self._rematch_log: list[dict] = []
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
        # Wrapping rows: a plain QHBoxLayout refuses to be narrower
        # than everything in it, which pinned the whole window's
        # minimum width at about 2000px -- on a smaller screen its
        # edges ended up off the desktop.
        chip_layout = FlowLayout(spacing=6)
        show_label = QLabel("Show")
        show_label.setObjectName("muted")
        chip_layout.addWidget(show_label)
        for category in CATEGORY_ORDER:
            chip = QToolButton()
            chip.setCheckable(True)
            # Every category is listed by default, "Moved" included: a
            # moved row can be the only place a relocated passage is
            # mentioned, so hiding it by default would quietly take
            # that passage off the review. One click still hides it.
            chip.setChecked(True)
            chip.setProperty("chip", True)
            chip.setToolTip(
                _MOVED_CHIP_TOOLTIP
                if category == _MOVED_CATEGORY
                else f"Show or hide '{CATEGORY_LABELS[category]}' differences"
            )
            chip.toggled.connect(self._refresh_table)
            self._chips[category] = chip
            chip_layout.addWidget(chip)
        self._update_chip_labels()

        # --- view options ---
        self.ignore_minor_checkbox = QCheckBox("Ignore minor and below")
        self.ignore_minor_checkbox.setToolTip(
            "Hides (does not delete) rows rated minor or below: spacing and "
            "punctuation, labels inside charts and diagrams, text-layer noise "
            "(a letter lost from a ligature or a doubled letter), and text "
            "found at another position. Every changed word or number, every "
            "added or deleted passage and every misspelling stays visible. "
            "Hidden rows are still in the report -- review them before sign-off."
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

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Sort: severity", "Sort: page order"])
        self.sort_combo.setToolTip(
            "Severity puts critical rows first. Page order walks the Original "
            "document from top to bottom, the way a proofreader reads."
        )
        self.sort_combo.currentIndexChanged.connect(self._refresh_table)

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

        options_layout = FlowLayout(spacing=10)
        options_layout.addWidget(self.ignore_minor_checkbox)
        options_layout.addWidget(self.group_checkbox)
        options_layout.addWidget(self.hide_reviewed_checkbox)
        options_layout.addWidget(self.hide_viewed_checkbox)
        options_layout.addWidget(self.sort_combo)
        options_layout.addWidget(self.search_box)
        options_layout.addWidget(self.unseen_badge)
        options_layout.addWidget(self.summary_label)
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

        self.coverage_banner = QLabel()
        self.coverage_banner.setObjectName("coverageBanner")
        self.coverage_banner.setWordWrap(True)
        self.coverage_banner.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.coverage_banner.setVisible(False)

        layout = QVBoxLayout()
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        layout.addWidget(self.coverage_banner)
        # Each wrapping row lives in its own widget, capped at the
        # height it actually needs: as a bare sub-layout the vertical
        # layout handed it the spare space, which came straight out of
        # the comparator below.
        for row in (chip_layout, options_layout):
            layout.addWidget(FlowRow(row))
        layout.addWidget(self.splitter)
        self.setLayout(layout)
        self.table.itemSelectionChanged.connect(self._update_comparator)
        self._update_action_buttons()
        theme.manager().changed.connect(lambda: self._refresh_table(keep_selection=True))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_warnings(self, warnings: list[str]) -> None:
        """Show what the comparison could NOT read (see
        core/coverage_check.py) above the list, until the next run."""
        self._warnings = list(warnings)
        if not warnings:
            self.coverage_banner.setVisible(False)
            return
        t = theme.tokens()
        self.coverage_banner.setStyleSheet(
            f"QLabel#coverageBanner {{ color: {t.danger}; border: 1px solid {t.danger};"
            " border-radius: 4px; padding: 6px 8px; }"
        )
        self.coverage_banner.setText(
            "<b>\u26a0 Not everything could be compared.</b><br>"
            + "<br>".join("\u2022 " + w.replace("&", "&amp;").replace("<", "&lt;") for w in warnings)
        )
        self.coverage_banner.setVisible(True)

    def warnings(self) -> list[str]:
        return list(getattr(self, "_warnings", []))

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

    def set_results(self, diff_items: list[DiffItem], keep_review: bool = False) -> None:
        """Replace the list with a fresh set of results.

        A new comparison starts with no decisions. ``keep_review`` is for
        the authoritative list arriving at the END of a comparison whose
        rows were already shown while it ran: decisions and "viewed"
        marks the reviewer gave those rows are kept for every row still
        in the final list (they used to be silently wiped).
        """
        if keep_review:
            kept = {id(item) for item in diff_items}
            self._decisions = {k: v for k, v in self._decisions.items() if k in kept}
            self._viewed_items = {k for k in self._viewed_items if k in kept}
        else:
            self._decisions = {}
            self._viewed_items = set()
            self._rematch_log = []
        self._items = diff_items
        self._categories = {id(item): category_of(item) for item in diff_items}
        self._update_chip_labels()
        self._refresh_table()

    def append_results(self, diff_items: list[DiffItem]) -> None:
        """Add differences found so far, while the comparison is still
        running, so the list fills as they are found rather than all at
        once at the end.

        The new rows are APPENDED to the table rather than the table
        being rebuilt: rebuilding costs as much as the whole first fill
        every time, so a long comparison arriving in sixty batches spent
        seconds redoing work and the list stuttered the whole way
        through. (Grouping identical rows does need a rebuild, since a
        new difference can join an existing group.)

        These rows are provisional -- the comparison's closing passes
        can still merge or re-rank them -- so `set_results` replaces the
        lot when it finishes.
        """
        if not diff_items:
            return
        self._items.extend(diff_items)
        for item in diff_items:
            self._categories[id(item)] = category_of(item)
        self._update_chip_labels()
        if self.group_checkbox.isChecked() or self._page_order():
            # Grouping can merge a new row into an existing one, and page
            # order can put it anywhere: both need the full rebuild.
            self._refresh_table(keep_selection=True)
            return

        listed = [item for item in diff_items if self._passes_filters(item)]
        if listed:
            start = self.table.rowCount()
            self._programmatic = True
            self.table.setUpdatesEnabled(False)
            try:
                self.table.setRowCount(start + len(listed))
                for offset, item in enumerate(listed):
                    group = [item]
                    self._row_groups.append(group)
                    self._visible_items.append(item)
                    self._fill_row(start + offset, group)
            finally:
                self.table.setUpdatesEnabled(True)
                self._programmatic = False
        self._update_summary_labels(len(self._visible_items))
        self._update_action_buttons()
        # No `view_changed` here: redrawing every difference on both
        # documents for each batch is far more work than the comparison
        # itself, and the highlights are drawn once at the end anyway.

    def clear_results(self) -> None:
        self.set_results([])
        self._rematches = []

    def replace_item(
        self, item: DiffItem, replacements: list[DiffItem], reason: str = ""
    ) -> list[DiffItem]:
        """Swap one difference for the rows a rematch produced, in its
        place in the list. Returns the new rows (empty when the rematch
        left nothing to report). Undone by `undo_rematch`. Every rematch
        is logged (`rematch_log`), so a removed row stays on record."""
        try:
            position = next(i for i, existing in enumerate(self._items) if existing is item)
        except StopIteration:
            return []
        self._log_rematch(
            item,
            "removed" if not replacements else f"replaced by {len(replacements)} row(s)",
            reason,
        )
        self._rematches.append((position, item, list(replacements)))
        self._items[position : position + 1] = replacements
        self._decisions.pop(id(item), None)
        self._categories.pop(id(item), None)
        self._viewed_items.discard(id(item))
        for new_item in replacements:
            self._categories[id(new_item)] = category_of(new_item)
        self._renumber()
        self._update_chip_labels()
        self._refresh_table()
        self.review_changed.emit()
        return replacements

    def undo_rematch(self) -> DiffItem | None:
        """Put the most recently rematched difference back. Returns it,
        or None when nothing has been rematched."""
        if not self._rematches:
            return None
        position, item, replacements = self._rematches.pop()
        self._log_rematch(item, "rematch undone (row restored)", "")
        start = next(
            (i for i, existing in enumerate(self._items)
             if any(existing is new_item for new_item in replacements)),
            position,
        )
        remaining = [i for i in self._items if not any(i is r for r in replacements)]
        remaining.insert(min(start, len(remaining)), item)
        self._items = remaining
        self._categories = {id(i): self._categories.get(id(i)) or category_of(i) for i in self._items}
        self._renumber()
        self._update_chip_labels()
        self._refresh_table()
        self.review_changed.emit()
        return item

    def _log_rematch(self, item: DiffItem, outcome: str, reason: str) -> None:
        self._rematch_log.append({
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "reviewer": self.reviewer,
            "outcome": outcome,
            "severity": item.severity,
            "approved_text": item.approved_text,
            "suspect_text": item.suspect_text,
            "reason": reason,
        })

    def rematch_log(self) -> list[dict]:
        return list(self._rematch_log)

    # --- keeping the review (core/review_store.py) -------------------------

    def review_state(self) -> dict:
        """Decisions, "viewed" marks and the rematch log, keyed by what
        each difference IS rather than where it sits in the list."""
        keys = fingerprints(self._items)
        decisions = {}
        viewed = []
        for key, item in zip(keys, self._items):
            decision = self._decisions.get(id(item))
            if decision is not None:
                decisions[key] = {
                    "status": decision.status,
                    "reviewer": decision.reviewer,
                    "decided_at": decision.decided_at.isoformat(),
                }
            if id(item) in self._viewed_items:
                viewed.append(key)
        return {"version": 1, "decisions": decisions, "viewed": viewed, "rematch_log": self._rematch_log}

    def restore_review(self, state: dict) -> int:
        """Put back a saved review for these same two documents. Returns
        how many decisions were restored."""
        if not state:
            return 0
        saved = state.get("decisions") or {}
        viewed = set(state.get("viewed") or [])
        restored = 0
        for key, item in zip(fingerprints(self._items), self._items):
            entry = saved.get(key)
            if entry and id(item) not in self._decisions:
                try:
                    decided_at = datetime.fromisoformat(entry["decided_at"])
                except (KeyError, ValueError):
                    decided_at = datetime.now(timezone.utc)
                self._decisions[id(item)] = Decision(entry.get("status", ""), entry.get("reviewer", ""), decided_at)
                restored += 1
            if key in viewed:
                self._viewed_items.add(id(item))
        if not self._rematch_log:
            self._rematch_log = list(state.get("rematch_log") or [])
        self._refresh_table(keep_selection=True)
        return restored

    def can_undo_rematch(self) -> bool:
        return bool(self._rematches)

    def _renumber(self) -> None:
        for index, item in enumerate(self._items):
            item.index = index

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

    def selected_items(self) -> list[DiffItem]:
        """Every difference in the selected rows (a grouped row stands
        for all its members)."""
        return self._selected_items()

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
                self._update_comparator(immediate=True)
                return True
        self._programmatic = True
        try:
            self.table.clearSelection()
        finally:
            self._programmatic = False
        self._update_action_buttons()
        self._update_comparator(immediate=True)
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
        self.review_changed.emit()

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
        self.review_changed.emit()

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
        self.review_changed.emit()

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
        if self._page_order():
            listed.sort(key=_page_position)
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

    def _page_order(self) -> bool:
        return self.sort_combo.currentIndex() == 1

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

    def _update_comparator(self, immediate: bool = False) -> None:
        """Show the current row's difference in the comparator (or clear
        it when nothing is selected).

        Held arrow keys and Next-difference walk through rows faster
        than the pane can be rebuilt -- each one costs a few
        milliseconds of HTML -- so a change from the table is coalesced
        on a short timer and only the row actually landed on is drawn.
        An explicit `select_item` (a difference clicked in a viewer)
        updates straight away.
        """
        if self._programmatic or not self.comparator.isVisible():
            return  # mid-rebuild; the caller updates once it settles
        if not immediate:
            self._comparator_timer.start()
            return
        self._comparator_timer.stop()
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
                self.review_changed.emit()
            self.item_activated.emit(self._visible_items[row])


def _page_position(item: DiffItem) -> tuple:
    """Where a difference sits, Original first: (page, top, left).
    Insertions have only a caret on the Original side; a row with no
    Original position at all sorts by its Suspect position, after."""
    for location, caret, side in (
        (item.approved_location, item.approved_caret, 0),
        (item.suspect_location, item.suspect_caret, 1),
    ):
        where = location or caret
        if where is not None:
            return (side, where.page, where.bbox[1], where.bbox[0])
    return (2, 0, 0.0, 0.0)
