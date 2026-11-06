"""Report the running QGIS application."""

from __future__ import annotations

from ...command import Command, CommandContext, Params, Result


class GetQGISInfoResult(Result):
    """The QGIS build that is running, and the plugin profile it uses."""

    qgis_version: str
    profile_folder: str
    plugins_count: int


class GetQGISInfo(Command):
    """Describe the QGIS build and the active plugin profile."""

    name = "get_qgis_info"

    def run(self, ctx: CommandContext, params: Params) -> GetQGISInfoResult:
        from qgis.core import Qgis, QgsApplication
        from qgis.utils import active_plugins

        return GetQGISInfoResult(
            qgis_version=Qgis.version(),
            profile_folder=QgsApplication.qgisSettingsDirPath(),
            plugins_count=len(active_plugins),
        )
