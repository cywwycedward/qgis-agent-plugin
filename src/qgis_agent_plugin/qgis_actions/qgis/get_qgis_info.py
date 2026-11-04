"""Report the running QGIS application."""

from __future__ import annotations

from typing import Any

from ..base import Action, ActionContext, BaseParams


class GetQGISInfo(Action):
    """Describe the QGIS build and the active plugin profile."""

    name = "get_qgis_info"

    def run(self, ctx: ActionContext, params: BaseParams) -> dict[str, Any]:
        from qgis.core import Qgis, QgsApplication
        from qgis.utils import active_plugins

        return {
            "qgis_version": Qgis.version(),
            "profile_folder": QgsApplication.qgisSettingsDirPath(),
            "plugins_count": len(active_plugins),
        }
