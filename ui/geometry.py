"""Shared, work-area-aware window geometry helpers.

Both the main window and the dialogs must fit the screen's *work area*
(taskbar excluded) with the window frame (title bar + borders) taken into
account - ``resize()``/``setGeometry()`` set the *client* rect, so ignoring
the frame is what made windows overflow and clip their bottom row.

Centralising the maths here means every window gets the same, correct
treatment instead of each one re-inventing it.
"""
from __future__ import annotations

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

SAFETY = 8        # px of breathing room kept between the frame and the work area


def available_geometry(widget) -> QRect:
    """Work area of the widget's screen (already excludes the taskbar)."""
    screen = widget.screen() or QApplication.primaryScreen()
    return screen.availableGeometry() if screen else QRect(0, 0, 1280, 760)


def frame_margins(widget) -> "tuple[int, int, int, int]":
    """(left, top, right, bottom) frame decorations, in pixels.

    ``geometry()`` is the client rect while ``frameGeometry()`` includes the
    chrome; the difference is what must also stay on screen.  Returns zeros
    before the widget has been shown."""
    fg, g = widget.frameGeometry(), widget.geometry()
    return (max(0, g.x() - fg.x()), max(0, g.y() - fg.y()),
            max(0, fg.right() - g.right()), max(0, fg.bottom() - g.bottom()))


def fit(widget, *, preferred=(1080, 760), minimum=(480, 360), center_on=None) -> None:
    """Size and centre `widget` so its whole frame fits the work area.

    `preferred` is the ideal client size; it is reduced as needed (never
    below `minimum`).  `center_on` is an optional widget whose frame is used
    as the centring reference (e.g. a dialog centring over its parent)."""
    avail = available_geometry(widget)
    ml, mt, mr, mb = frame_margins(widget)
    max_w = max(minimum[0], avail.width() - ml - mr - SAFETY)
    max_h = max(minimum[1], avail.height() - mt - mb - SAFETY)
    w = min(preferred[0], max_w)
    h = min(preferred[1], max_h)
    ref = center_on.frameGeometry() if center_on is not None else avail
    x = ref.x() + max(0, (ref.width() - (w + ml + mr)) // 2)
    y = ref.y() + max(0, (ref.height() - (h + mt + mb)) // 2)
    widget.setGeometry(x + ml, y + mt, w, h)
    clamp(widget)


def clamp(widget, *, margin=SAFETY) -> None:
    """Shrink and/or move `widget` so its whole frame stays inside the work
    area - e.g. after restoring an oversized saved geometry."""
    if widget.isMaximized() or widget.isFullScreen():
        return
    avail = available_geometry(widget)
    ml, mt, mr, mb = frame_margins(widget)
    g = widget.geometry()
    w = max(widget.minimumWidth(), min(g.width(), avail.width() - ml - mr - margin))
    h = max(widget.minimumHeight(), min(g.height(), avail.height() - mt - mb - margin))
    fw, fh = w + ml + mr, h + mt + mb
    fg = widget.frameGeometry()
    x = min(max(fg.x(), avail.x()), max(avail.x(), avail.x() + avail.width() - fw))
    y = min(max(fg.y(), avail.y()), max(avail.y(), avail.y() + avail.height() - fh))
    widget.setGeometry(x + ml, y + mt, w, h)