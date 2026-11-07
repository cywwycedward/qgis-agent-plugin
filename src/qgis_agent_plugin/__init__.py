"""The QGIS Agent plugin package.

QGIS looks the entry point up by name on the package, so ``classFactory`` stays
here and imports the plugin only when QGIS calls it: importing the package alone
stays cheap and pulls in no registry wiring, which is what lets every layer below
it be imported and tested on its own.
"""

from __future__ import annotations

from typing import Any


def classFactory(iface: Any) -> Any:
    from .plugin import Plugin

    return Plugin(iface)


__all__ = ["classFactory"]
