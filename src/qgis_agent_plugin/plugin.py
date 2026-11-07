"""The plugin QGIS loads: a registry built at ``initGui`` and dropped at ``unload``.

Nothing is wired to the engine yet, so the plugin's whole surface is the registry
it fills from the operations this build ships.  The pydantic major is checked
here, before the model layer can be imported: the DTOs are written against
pydantic 2, so any other major has to read as a dependency error rather than as
an import failure inside a model module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pydantic

if TYPE_CHECKING:
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

    def initGui(self) -> None:
        from .core.registry import OperationRegistry
        from .operations import register_all

        registry = OperationRegistry()
        register_all(registry)
        self.registry = registry

    def unload(self) -> None:
        self.registry = None


def classFactory(iface: Any) -> Plugin:
    return Plugin(iface)
