"""Line icons for buttons and actions (ui/icons.py).

One small set drawn on a 24-unit grid with round 1.8-unit strokes, so
every icon has the same weight. Each is rendered in the current theme's
colors -- normal, disabled, and "on" for checkable buttons -- and
re-rendered when the theme changes (`bind`).
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from ui import theme

# Shapes inside <svg viewBox="0 0 24 24">, stroked with the icon color.
_SHAPES = {
    "compare": '<rect x="3" y="4" width="7.5" height="16" rx="1.5"/><rect x="13.5" y="4" width="7.5" height="16" rx="1.5"/>'
               '<path d="M5.5 9h2.5M5.5 12.5h2.5M16 9h2.5M16 12.5h2.5"/>',
    "crop": '<path d="M6 2.5V16a2 2 0 0 0 2 2h13.5"/><path d="M18 21.5V8a2 2 0 0 0-2-2H2.5"/>',
    "chevron-left": '<path d="M15 18l-6-6 6-6"/>',
    "chevron-right": '<path d="M9 18l6-6-6-6"/>',
    "chevron-up": '<path d="M18 15l-6-6-6 6"/>',
    "chevron-down": '<path d="M6 9l6 6 6-6"/>',
    "rematch": '<path d="M20.5 12a8.5 8.5 0 1 1-2.5-6"/><path d="M20.5 3.5V9H15"/>',
    "area": '<rect x="3.5" y="3.5" width="17" height="17" rx="2" stroke-dasharray="3.2 2.6"/>'
            '<path d="M12 9v6M9 12h6"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7.5a4 4 0 0 1 8 0V11"/>',
    "eye": '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M3 3l18 18"/><path d="M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.1 4.1"/>'
               '<path d="M6.6 6.6A17 17 0 0 0 2 12s3.6 7 10 7a9.6 9.6 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    "blink": '<path d="M13 2.5L4 13.5h7l-1 8 9-11h-7l1-8z"/>',
    "folder": '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5H9l2 2.2h8.5A1.5 1.5 0 0 1 21 8.7v8.8a1.5 1.5 0 0 1-1.5 1.5h-15'
              'A1.5 1.5 0 0 1 3 17.5z"/>',
    "merge": '<path d="M12 3.5l8.5 4.5-8.5 4.5L3.5 8z"/><path d="M3.5 12.5l8.5 4.5 8.5-4.5"/><path d="M3.5 16.5l8.5 4.5 8.5-4.5"/>',
    "zoom-in": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M20.5 20.5l-5.3-5.3M10.5 7.5v6M7.5 10.5h6"/>',
    "zoom-out": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M20.5 20.5l-5.3-5.3M7.5 10.5h6"/>',
    "fit-width": '<path d="M3 12h18M7 8l-4 4 4 4M17 8l4 4-4 4"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M20.5 20.5l-5.3-5.3"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "x": '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    "reset": '<path d="M3.5 12a8.5 8.5 0 1 0 2.5-6"/><path d="M3.5 3.5V9H9"/>',
    "undo": '<path d="M9 14L4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4.5 20.5a7.5 7.5 0 0 1 15 0"/>',
    "export": '<path d="M12 3.5v11M7.5 10l4.5 4.5 4.5-4.5M5 20.5h14"/>',
    "file": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    "upload": '<path d="M12 16V4.5M7.5 9l4.5-4.5L16.5 9M5 20h14"/>',
    "table": '<rect x="3.5" y="4.5" width="17" height="15" rx="1.5"/><path d="M3.5 9.5h17M3.5 14.5h17M9.5 9.5v10M15 9.5v10"/>',
    "match": '<path d="M2.5 12.5l4 4L15 8"/><path d="M11.5 15.5l1 1L21 8"/>',
    "filter": '<path d="M3.5 5h17l-6.5 8v6l-4 2v-8z"/>',
}

# Which theme color an icon is drawn in.
_ROLES = ("text", "muted", "accent", "accent_text", "success", "danger")


def _svg(name: str, color: str) -> bytes:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
        f"{_SHAPES[name]}</svg>"
    ).encode()


def _pixmap(name: str, color: str, size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(_svg(name, color))).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


_cache: dict[tuple[str, str, bool], QIcon] = {}


def icon(name: str, role: str = "text") -> QIcon:
    """``name`` in the current theme: ``role`` is the color it is drawn
    in (a `theme.Tokens` field), disabled in the border color, and the
    accent color when a checkable button is on."""
    t = theme.tokens()
    key = (name, role, t.dark)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    color = getattr(t, role) if role in _ROLES else t.text
    result = QIcon()
    for size in (16, 20, 24, 32, 40, 48):
        result.addPixmap(_pixmap(name, color, size), QIcon.Mode.Normal, QIcon.State.Off)
        result.addPixmap(_pixmap(name, t.border_strong, size), QIcon.Mode.Disabled, QIcon.State.Off)
        result.addPixmap(_pixmap(name, t.accent if role == "text" else color, size), QIcon.Mode.Normal, QIcon.State.On)
    _cache[key] = result
    return result


def pixmap(name: str, size: int, role: str = "muted") -> QPixmap:
    """A single rendered icon, e.g. for an empty-state illustration."""
    t = theme.tokens()
    return _pixmap(name, getattr(t, role, t.muted), size)


def bind(target, name: str, role: str = "text") -> None:
    """Give a QAction or button ``name`` now and again on every theme
    change."""

    def apply() -> None:
        try:
            target.setIcon(icon(name, role))
        except RuntimeError:  # the widget is gone
            pass

    apply()
    theme.manager().changed.connect(apply)
