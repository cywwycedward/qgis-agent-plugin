"""The one place that reads QGIS globals and hands them to the actions."""

from __future__ import annotations

from typing import Any

from .qgis_actions import ActionContext
from .qgis_operation_manager import QGISOperationManager


class PluginRuntime:
    """Build the per-command context for a running ``iface``.

    The operation manager is created once per plugin run: asynchronous commands
    register into it and the polling commands read it back, so a manager built
    per command would forget every operation as soon as it was acknowledged.
    """

    def __init__(self, iface: Any) -> None:
        self.iface = iface
        self.operations = QGISOperationManager()

    def context(self) -> ActionContext:
        # Deferred so this module stays importable without QGIS.
        from qgis.core import QgsProject

        return ActionContext(
            iface=self.iface, project=QgsProject.instance(), operations=self.operations
        )
