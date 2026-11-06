"""Colours the dock and the debug page share, picked for the theme QGIS runs."""

from __future__ import annotations

from qgis.PyQt.QtGui import QPalette
from qgis.PyQt.QtWidgets import QApplication


def outcome_colors() -> tuple[str, str]:
    """Green and red for an outcome line, readable in either theme.

    A fixed pair would be unreadable in the other theme, and these carry meaning,
    so they come from the theme's own lightness rather than from a gray.
    """

    dark = QApplication.palette().color(QPalette.Window).lightness() < 128
    return ("#4ade80", "#f87171") if dark else ("#15803d", "#b91c1c")
