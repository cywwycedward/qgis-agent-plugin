"""The plugin QGIS loads: a registry built at ``initGui`` and dropped at ``unload``.

Nothing is wired to the engine yet, so the plugin's whole surface is the registry
it fills from the operations this build ships.  Building it here, once, keeps
registration a start-up step (a broken declaration fails then, not on a call) and
gives the engine a single place to be handed the same registry later.
"""

from __future__ import annotations

from typing import Any

from .core.registry import OperationRegistry
from .operations import register_all


class Plugin:
    """The instance QGIS keeps alive between ``initGui`` and ``unload``."""

    def __init__(self, iface: Any) -> None:
        self.iface = iface
        self.registry: OperationRegistry | None = None

    def initGui(self) -> None:
        registry = OperationRegistry()
        register_all(registry)
        self.registry = registry

    def unload(self) -> None:
        self.registry = None


def classFactory(iface: Any) -> Plugin:
    return Plugin(iface)
