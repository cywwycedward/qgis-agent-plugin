"""The plugin QGIS loads: registry and engine at ``initGui``, dropped at ``unload``.

The plugin's surface is the registry it fills from the operations this build ships
and the engine that dispatches them.  The pydantic major is checked here, before
the model layer can be imported: the DTOs are written against pydantic 2, so any
other major has to read as a dependency error rather than as an import failure
inside a model module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pydantic

if TYPE_CHECKING:
    from .core.engine import OperationEngine
    from .core.registry import OperationRegistry


def require_supported_pydantic(version: str) -> None:
    """Refuse a pydantic major the DTO layer cannot import from (PRD 6.1)."""

    if int(version.split(".", 1)[0]) != 2:
        raise RuntimeError(f"qgis-agent-plugin requires pydantic 2.x, but {version} is installed")


require_supported_pydantic(pydantic.VERSION)


class Plugin:
    """The instance QGIS keeps alive between ``initGui`` and ``unload``."""

    def __init__(self, iface: Any) -> None:
        self.iface = iface
        self.registry: OperationRegistry | None = None
        self.engine: OperationEngine | None = None

    def initGui(self) -> None:
        from qgis.core import QgsProject

        from .core.config import Settings
        from .core.context import ProjectLifecycle
        from .core.engine import OperationEngine
        from .core.registry import OperationRegistry
        from .core.runners.qgs_task import OperationTaskRunner
        from .operations import register_all

        registry = OperationRegistry()
        register_all(registry)
        self.registry = registry
        # The adapter is built before the engine because the engine's seam needs it;
        # it holds no reference to the engine between calls.
        runner = OperationTaskRunner()
        # Settings stay at their defaults until the lifecycle reads QgsSettings;
        # the epoch starts at 0 until it is bound to aboutToBeCleared.
        self.engine = OperationEngine(
            registry,
            Settings(),
            ProjectLifecycle(),
            iface=self.iface,
            project=QgsProject.instance(),
            background_runner=runner,
            background_cancel=runner.cancel,
        )

    def unload(self) -> None:
        if self.engine is not None:
            self.engine.shutdown()
        self.engine = None
        self.registry = None


def classFactory(iface: Any) -> Plugin:
    return Plugin(iface)
