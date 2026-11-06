"""Report the current project and a bounded sample of its layers."""

from __future__ import annotations

from ...command import Command, CommandContext, Params, Result
from ..layer.report import LayerReport, layer_report, layers_in_tree_order

# The report stays bounded so a large project cannot flood a single reply.
LAYER_LIMIT = 10


class GetProjectInfoResult(Result):
    """The project QGIS holds, and the first layers of its tree."""

    filename: str
    title: str
    layer_count: int
    crs: str
    layers: list[LayerReport]


class GetProjectInfo(Command):
    """Describe the project: file, title, CRS, layer count and a sample."""

    name = "get_project_info"

    def run(self, ctx: CommandContext, params: Params) -> GetProjectInfoResult:
        project = ctx.project
        layers = layers_in_tree_order(project)
        return GetProjectInfoResult(
            filename=project.fileName(),
            title=project.title(),
            layer_count=len(layers),
            crs=project.crs().authid(),
            layers=[layer_report(entry.layer, entry.node) for entry in layers[:LAYER_LIMIT]],
        )
