"""The QGIS Agent plugin package.

QGIS looks the entry point up by name on the package, so the import of the GUI
module stays inside ``classFactory``: importing this package must not pull Qt in,
or every module under it -- including the ones that never touch QGIS -- becomes
impossible to import, and so to test, in a plain interpreter.
"""

from __future__ import annotations

from typing import Any


def classFactory(iface: Any) -> Any:
    from .plugin import Plugin

    return Plugin(iface)


__all__ = ["classFactory"]
