"""A layout whose items wrap onto the next line (ui/flow_layout.py).

A plain QHBoxLayout refuses to be narrower than the sum of everything
in it. The deviation list's filter chips and view options are a long
row of small controls, so that sum was about 2000px: the window could
not be made any narrower, and on a smaller screen its right-hand side
and the bottom of the Deviations dock ended up off the desktop.

This lays the same controls out left to right and starts a new line
when the next one would not fit, so the panel's minimum width is one
control instead of all of them.

Qt ships this as an example rather than a class; this is that layout,
kept small and documented.
"""

from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QSizePolicy, QWidget, QWidgetItem


class FlowLayout(QLayout):
    def __init__(self, parent=None, margin: int = 0, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list[QWidgetItem] = []
        self.setContentsMargins(QMargins(margin, margin, margin, margin))
        self.setSpacing(spacing)

    # --- QLayout plumbing ---------------------------------------------

    def addItem(self, item) -> None:  # noqa: N802 -- Qt override
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802 -- Qt override
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802 -- Qt override
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802 -- Qt override
        return Qt.Orientation(0)

    # --- height-for-width ---------------------------------------------

    def hasHeightForWidth(self) -> bool:  # noqa: N802 -- Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 -- Qt override
        return self._lay_out(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 -- Qt override
        super().setGeometry(rect)
        self._lay_out(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802 -- Qt override
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 -- Qt override
        # One item wide: everything else can wrap below it.
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    # --- the layout itself ---------------------------------------------

    def _lay_out(self, rect: QRect, apply: bool) -> int:
        """Place the items inside ``rect``; returns the height used."""
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, line_height = area.x(), area.y(), 0
        spacing = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            if x > area.x() and x + hint.width() > area.right() + 1:
                x = area.x()
                y += line_height + spacing
                line_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + spacing
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


class FlowRow(QWidget):
    """A widget holding a `FlowLayout`, no taller than the rows need.

    A bare sub-layout inside a QVBoxLayout is handed the spare vertical
    space (which here came straight out of the comparator below it),
    and a plain QWidget does not ask its layout how tall it needs to be
    for the width it was given. This does both: it keeps itself exactly
    as tall as its wrapped rows.
    """

    def __init__(self, layout: FlowLayout, parent=None) -> None:
        super().__init__(parent)
        self.setLayout(layout)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 -- Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 -- Qt override
        return self.layout().heightForWidth(width)

    def sizeHint(self) -> QSize:  # noqa: N802 -- Qt override
        width = self.width() or self.layout().sizeHint().width()
        return QSize(width, self.heightForWidth(max(width, 1)))

    def resizeEvent(self, event) -> None:  # noqa: N802 -- Qt override
        super().resizeEvent(event)
        needed = self.heightForWidth(max(self.width(), 1))
        if needed != self.height():
            self.setFixedHeight(needed)
