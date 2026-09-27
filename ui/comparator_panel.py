"""Comparator pane (ui/comparator_panel.py).

The right-hand side of the Deviations dock: the difference selected in
the list, spelled out. The list says THAT a passage differs; this pane
says WHAT in it differs, the way a proofreading station does --

    ORIGINAL (APPROVED)
    ... Store below 25 C. Keep out of reach of children ...
                    ^^ marked
    SUSPECT
    ... Store below 30 C. Keep out of reach of children ...
                    ^^ marked

Three things are shown, top to bottom:

  * a header -- severity badge, category, and the page each side sits
    on ("Original page 4 - Suspect page 1"), plus the review decision
    and, for a grouped row, how many times this same difference
    occurs;
  * the Original text and the Suspect text, each with the exact words
    (and, inside an edited word, the exact characters) that differ
    marked -- red on the Original for what was removed or replaced,
    green on the Suspect for what replaced it. Surrounding words from
    the document are shown dimmed around the difference so it can be
    read in its sentence; a space that is itself part of the
    difference is drawn as a middle dot, since an invisible change
    would otherwise look like no change at all;
  * a one-line summary naming just the words that differ
    ("Ropinirole 5  ->  Ropinirol 50").

Word/character splitting is `core.inline_diff`; the surrounding words
come from the comparison's own `core.token_map.TokenMap`, so they are
that document's real neighboring words, not a re-read of the file.
Colors come from ui/theme.py and follow the light/dark theme.
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.diff_engine import CATEGORY_LABELS, DiffItem, Location, category_of
from core.inline_diff import Segment, changed_words, inline_segments
from core.token_map import APPROVED, SUSPECT, TokenMap
from ui import theme

_SEVERITY_LABEL = {"reorder": "Moved"}
# A space inside a changed run is drawn as this, so a spacing-only
# difference is actually visible.
_VISIBLE_SPACE = "·"
_ELLIPSIS = "…"

_EMPTY_MESSAGE = "Select a difference to see what changed."
_MISSING_SIDE = {
    "removed_in_suspect": "Not present in the Suspect document.",
    "added_in_suspect": "Not present in the Original document.",
}


def _mix(color: str, background: str, ratio: float) -> str:
    """``color`` laid over ``background`` at ``ratio`` opacity, as
    #rrggbb -- a tint, so marked text stays readable in both themes."""
    top, bottom = QColor(color), QColor(background)
    mixed = QColor(
        round(top.red() * ratio + bottom.red() * (1 - ratio)),
        round(top.green() * ratio + bottom.green() * (1 - ratio)),
        round(top.blue() * ratio + bottom.blue() * (1 - ratio)),
    )
    return mixed.name()


def _escape(text: str) -> str:
    return html.escape(text).replace("\n", "<br>")


def _escape_marked(text: str) -> str:
    """A changed run. A run that is nothing BUT whitespace is drawn as
    middle dots -- otherwise a spacing-only difference would be marked
    invisibly; runs with real words keep their ordinary spaces."""
    if text and not text.strip():
        return _VISIBLE_SPACE * len(text)
    return _escape(text)


class ComparatorPanel(QWidget):
    """Shows one DiffItem's Original and Suspect text with the changed
    words marked. `show_item` fills it; `clear` returns it to its empty
    state. Display only -- it never changes a review decision."""

    occurrence_selected = Signal(object)  # DiffItem

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("comparator")
        self._token_map: TokenMap | None = None
        self._item: DiffItem | None = None
        self._items: list[DiffItem] = []
        self._occurrence_idx: int = 0
        self._count = 1
        self._status = ""

        self.severity_label = QLabel()
        self.severity_label.setObjectName("severityBadge")
        self.severity_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.category_label = QLabel()
        self.category_label.setObjectName("comparatorCategory")
        self.pages_label = QLabel()
        self.pages_label.setObjectName("muted")
        self._show_context = False
        self.context_checkbox = QCheckBox("Context")
        self.context_checkbox.setChecked(False)
        self.context_checkbox.setStyleSheet("color: #888888; font-size: 11px;")
        self.context_checkbox.setToolTip("Show a few words of surrounding context around the difference.")
        self.context_checkbox.toggled.connect(self._on_context_toggled)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(self.severity_label)
        header.addWidget(self.category_label)
        header.addStretch()
        header.addWidget(self.context_checkbox)
        header.addWidget(self.pages_label)

        # Stepper bar to navigate across all repeated occurrences
        self.occurrence_bar = QWidget()
        occ_layout = QHBoxLayout(self.occurrence_bar)
        occ_layout.setContentsMargins(0, 2, 0, 2)
        occ_layout.setSpacing(6)

        self.occ_prev_btn = QToolButton()
        self.occ_prev_btn.setText("◀")
        self.occ_prev_btn.setToolTip("Jump to previous occurrence in the document")
        self.occ_prev_btn.clicked.connect(self._prev_occurrence)

        self.occ_label = QLabel()
        self.occ_label.setStyleSheet("font-weight: 600; color: #3b82f6;")

        self.occ_next_btn = QToolButton()
        self.occ_next_btn.setText("▶")
        self.occ_next_btn.setToolTip("Jump to next occurrence in the document")
        self.occ_next_btn.clicked.connect(self._next_occurrence)

        occ_layout.addWidget(self.occ_prev_btn)
        occ_layout.addWidget(self.occ_label)
        occ_layout.addWidget(self.occ_next_btn)
        occ_layout.addStretch()
        self.occurrence_bar.setVisible(False)

        self.status_label = QLabel()
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)

        self.original_view = self._make_text_view()
        self.suspect_view = self._make_text_view()
        self.summary_label = QLabel()
        self.summary_label.setObjectName("muted")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        layout = QVBoxLayout()
        layout.setContentsMargins(12, 8, 6, 6)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self.occurrence_bar)
        layout.addWidget(self.status_label)
        layout.addWidget(self._caption("Original (approved)"))
        layout.addWidget(self.original_view, 1)
        layout.addWidget(self._caption("Suspect"))
        layout.addWidget(self.suspect_view, 1)
        layout.addWidget(self.summary_label)
        self.setLayout(layout)
        self.setMinimumWidth(240)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        theme.manager().changed.connect(self._restyle)
        self.clear()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_token_map(self, token_map: TokenMap | None) -> None:
        """Use ``token_map`` (from the last comparison) for the
        surrounding words. Without one, only the differing text shows."""
        self._token_map = token_map

    def show_item(
        self,
        item: DiffItem,
        count: int = 1,
        status: str = "",
        all_items: list[DiffItem] | None = None,
    ) -> None:
        """Show ``item``. ``count`` is how many identical differences the
        selected row stands for; ``all_items`` are all identical occurrences."""
        self._items = all_items or [item]
        self._occurrence_idx = 0
        self._item = self._items[0]
        self._count = len(self._items)
        self._status = status
        self._restyle()

    def clear(self) -> None:
        self._item = None
        self._items = []
        self._occurrence_idx = 0
        self._count = 1
        self._status = ""
        self._restyle()

    def _prev_occurrence(self) -> None:
        if self._items and self._occurrence_idx > 0:
            self._occurrence_idx -= 1
            self._item = self._items[self._occurrence_idx]
            self._restyle()
            self.occurrence_selected.emit(self._item)

    def _next_occurrence(self) -> None:
        if self._items and self._occurrence_idx + 1 < len(self._items):
            self._occurrence_idx += 1
            self._item = self._items[self._occurrence_idx]
            self._restyle()
            self.occurrence_selected.emit(self._item)

    def current_item(self) -> DiffItem | None:
        return self._item

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _make_text_view(self) -> QTextBrowser:
        view = QTextBrowser()
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.setOpenExternalLinks(False)
        view.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setMinimumHeight(52)
        return view

    def _caption(self, text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setObjectName("comparatorCaption")
        return label

    def _on_context_toggled(self, checked: bool) -> None:
        self._show_context = checked
        self._restyle()

    def _restyle(self) -> None:
        """Redraw for the current item and theme (also the empty state)."""
        t = theme.tokens()
        self.setStyleSheet(
            f"#comparator {{ background: {t.surface}; border-left: 1px solid {t.border}; }}"
            f"#comparatorCaption {{ color: {t.muted}; font-size: 10px; font-weight: 600; }}"
            f"#comparatorCategory {{ font-weight: 600; color: {t.text}; }}"
            f"QTextBrowser {{ background: {t.surface}; border: none; }}"
        )
        item = self._item
        if item is None:
            self.severity_label.setVisible(False)
            self.category_label.setText("No difference selected")
            self.pages_label.clear()
            self.status_label.clear()
            self.status_label.setVisible(False)
            self.summary_label.clear()
            self.original_view.setHtml(self._html([], "", "", "", muted_message=_EMPTY_MESSAGE))
            self.suspect_view.setHtml(self._html([], "", "", ""))
            return

        fg, bg = t.severity(item.severity)
        self.severity_label.setVisible(True)
        self.severity_label.setText(_SEVERITY_LABEL.get(item.severity, item.severity.capitalize()))
        self.severity_label.setStyleSheet(
            f"#severityBadge {{ color: {fg}; background: {bg}; border-radius: 3px;"
            f" padding: 1px 7px; font-weight: 600; }}"
        )
        self.category_label.setText(CATEGORY_LABELS[category_of(item)])
        self.pages_label.setText(self._pages_text(item))

        if len(self._items) > 1:
            self.occurrence_bar.setVisible(True)
            curr_occ = self._occurrence_idx + 1
            tot_occ = len(self._items)
            self.occ_label.setText(f"Occurrence {curr_occ} of {tot_occ}")
            self.occ_prev_btn.setEnabled(curr_occ > 1)
            self.occ_next_btn.setEnabled(curr_occ < tot_occ)
        else:
            self.occurrence_bar.setVisible(False)

        notes = []
        if self._count > 1:
            def pgs_of(loc_attr):
                pgs = []
                for it in self._items:
                    loc = getattr(it, loc_attr, None)
                    if loc is not None:
                        for p in (sorted(loc.boxes_by_page) if getattr(loc, "boxes_by_page", None) else [loc.page]):
                            if p not in pgs:
                                pgs.append(p)
                return sorted(pgs)

            orig_pgs = pgs_of("approved_location") or pgs_of("approved_caret")
            susp_pgs = pgs_of("suspect_location") or pgs_of("suspect_caret")
            pg_parts = []
            if orig_pgs:
                pg_parts.append(f"Original pages: {', '.join(map(str, orig_pgs))}")
            if susp_pgs:
                pg_parts.append(f"Suspect pages: {', '.join(map(str, susp_pgs))}")

            pages_summary = f"Occurs {self._count}× in the document"
            if pg_parts:
                pages_summary += f" ({' · '.join(pg_parts)})"
            notes.append(pages_summary + " — one decision covers all.")

        if self._status:
            notes.append(self._status)
        self.status_label.setText("   ".join(notes))
        self.status_label.setVisible(bool(notes))

        approved_segments, suspect_segments = inline_segments(item.approved_text, item.suspect_text)
        self.original_view.setHtml(
            self._side_html(item, APPROVED, approved_segments, t.danger, item.approved_location)
        )
        self.suspect_view.setHtml(
            self._side_html(item, SUSPECT, suspect_segments, t.success, item.suspect_location)
        )
        self.summary_label.setText(self._summary(item))

    def _pages_text(self, item: DiffItem) -> str:
        def page_of(location: Location | None, caret: Location | None) -> str:
            if location is not None:
                pages = sorted(location.boxes_by_page) or [location.page]
                if len(pages) > 1:
                    return f"pages {pages[0]}–{pages[-1]}"
                return f"page {pages[0]}"
            if caret is not None:
                return f"page {caret.page}"
            return "—"

        approved = page_of(item.approved_location, item.approved_caret)
        suspect = page_of(item.suspect_location, item.suspect_caret)
        return f"Original {approved}  ·  Suspect {suspect}"

    def _side_html(
        self,
        item: DiffItem,
        side: str,
        segments: list[Segment],
        mark_color: str,
        location: Location | None,
    ) -> str:
        if not segments:
            return self._html(
                [], "", "", mark_color, muted_message=_MISSING_SIDE.get(item.kind, "—")
            )
        before, after = self._context(item, side, location) if self._show_context else ("", "")
        return self._html(segments, before, after, mark_color)

    def _context(self, item: DiffItem, side: str, location: Location | None) -> tuple[str, str]:
        """The document's own words around this difference, dimmed in the
        display. Empty when there is no comparison to read them from."""
        if self._token_map is None:
            return "", ""
        try:
            if location is not None:
                return self._token_map.context_words(side, location)
            caret = item.approved_caret if side == APPROVED else item.suspect_caret
            return self._token_map.context_at_caret(side, caret)
        except (KeyError, IndexError):  # a token map from a different comparison
            return "", ""

    def _html(
        self,
        segments: list[Segment],
        before: str,
        after: str,
        mark_color: str,
        muted_message: str = "",
    ) -> str:
        t = theme.tokens()
        if muted_message or not segments:
            style = f"color: {t.muted}; font-size: 13px; font-style: italic;"
            return f"<div style='{style}'>{_escape(muted_message)}</div>"
        style = f"color: {t.text}; font-size: 13px; line-height: 150%;"
        tint = _mix(mark_color, t.surface, 0.28 if t.dark else 0.18)
        parts = []
        if before:
            parts.append(f"<span style='color: {t.muted}'>{_ELLIPSIS} {_escape(before)} </span>")
        for changed, text in segments:
            if changed:
                parts.append(
                    f"<span style='background-color: {tint}; color: {mark_color};"
                    f" font-weight: 600'>{_escape_marked(text)}</span>"
                )
            else:
                parts.append(_escape(text))
        if after:
            parts.append(f"<span style='color: {t.muted}'> {_escape(after)} {_ELLIPSIS}</span>")
        return f"<div style='{style}'>{''.join(parts)}</div>"

    def _summary(self, item: DiffItem) -> str:
        """One line naming just the words that differ."""
        left, right = changed_words(item.approved_text, item.suspect_text)
        if item.severity == "reorder" and not left and not right:
            # Nothing in the text changed -- where it sits did.
            return "Same text — moved to a different position in the document."
        if not left and not right:
            # Everything that differs is whitespace.
            return "Spacing only — the words themselves are identical."
        if not left:
            return f"Inserted:   {right}"
        if not right:
            return f"Deleted on suspect:   {left}"
        return f"{left}   →   {right}"
