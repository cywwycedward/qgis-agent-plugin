"""The one place that reads QGIS globals and hands them to the actions."""

from __future__ import annotations

from typing import Any

from .operation_registry import OperationRegistry
from .qgis_actions import ActionContext


class PluginRuntime:
    """Build the per-command context for a running ``iface``.

    The registry is created once per plugin run: asynchronous commands register
    into it and the polling commands read it back, so a registry built per
    command would forget every operation as soon as it was acknowledged.
    """

    def __init__(self, iface: Any) -> None:
        self.iface = iface
        self.operations = OperationRegistry()

    def context(self) -> ActionContext:
        # Deferred so this module stays importable without QGIS.
        from qgis.core import QgsProject

        return ActionContext(
            iface=self.iface, project=QgsProject.instance(), operations=self.operations
        )
