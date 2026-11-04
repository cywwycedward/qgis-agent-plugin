"""Report the current project and a bounded sample of its layers."""

from __future__ import annotations

from typing import Any

from ..base import Action, ActionContext, BaseParams
from ..layer.base import layer_report, layers_in_tree_order

# The report stays bounded so a large project cannot flood a single reply.
LAYER_LIMIT = 10


class GetProjectInfo(Action):
    """Describe the project: file, title, CRS, layer count and a sample."""

    name = "get_project_info"

    def run(self, ctx: ActionContext, params: BaseParams) -> dict[str, Any]:
        project = ctx.project
        layers = layers_in_tree_order(project)
        return {
            "filename": project.fileName(),
            "title": project.title(),
            "layer_count": len(layers),
            "crs": project.crs().authid(),
            "layers": [
                layer_report(entry.layer, entry.node).model_dump(mode="json")
                for entry in layers[:LAYER_LIMIT]
            ],
        }
