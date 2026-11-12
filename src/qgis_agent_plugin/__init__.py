"""QGIS Agent 插件的包入口。

QGIS 按名字在包上找入口函数，所以 ``classFactory`` 留在这里，且只在 QGIS 调用它时才导入
``plugin``：单是导入这个包仍然很轻、不牵动注册表接线，下面每一层因此都能单独导入与测试。

The QGIS Agent plugin package.

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
