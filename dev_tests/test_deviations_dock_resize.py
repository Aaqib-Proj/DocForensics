import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from main import MainWindow, _DOCK_HEIGHT


@pytest.fixture(scope="module")
def app():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication(sys.argv)
    return instance


def test_central_widget_allows_dock_expansion_via_cursor(app):
    win = MainWindow()
    win.resize(1536, 793)
    win.show()
    app.processEvents()

    # Trigger warning banner
    win.results_panel.set_warnings(["Suspect: page 31 has no extractable text."])
    app.processEvents()

    # Ensure centralWidget minimum height is low enough (<= 250px)
    assert win.centralWidget().minimumSizeHint().height() <= 250

    # Test that dragging the separator upwards actually expands the dock
    before_dock_h = win.results_dock.height()
    sep_y = (win.centralWidget().geometry().bottom() + win.results_dock.geometry().top()) // 2

    # Simulate mouse drag upwards by 100 pixels
    press = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(500, sep_y),
        QPointF(500, sep_y),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    app.sendEvent(win, press)
    app.processEvents()

    move = QMouseEvent(
        QMouseEvent.Type.MouseMove,
        QPointF(500, sep_y - 100),
        QPointF(500, sep_y - 100),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    app.sendEvent(win, move)
    app.processEvents()

    release = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        QPointF(500, sep_y - 100),
        QPointF(500, sep_y - 100),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    app.sendEvent(win, release)
    app.processEvents()

    after_dock_h = win.results_dock.height()
    assert after_dock_h >= before_dock_h + 50, (
        f"Dock failed to expand via cursor drag: before={before_dock_h}, after={after_dock_h}"
    )


def test_coverage_banner_dismiss(app):
    win = MainWindow()
    win.show()
    app.processEvents()
    win.results_panel.set_warnings(["Test warning message"])
    app.processEvents()
    assert win.results_panel.coverage_banner.isVisible()
    assert "Test warning message" in win.results_panel.coverage_banner.text()

    # Click the dismiss button
    win.results_panel.coverage_close_btn.click()
    app.processEvents()
    assert not win.results_panel.coverage_banner.isVisible()
