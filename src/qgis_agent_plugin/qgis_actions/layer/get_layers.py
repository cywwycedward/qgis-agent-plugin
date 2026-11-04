"""List the project's layers, in the order the layer tree lists them.

The order is the layer tree's -- what the legend shows -- so reading the answer
top-down is reading the stack the canvas draws.  A project can override that
order for the canvas alone (``hasCustomLayerOrder``); this command answers in the
tree's order either way, because the tree is what both the caller and the legend
look at.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ..base import Action, ActionContext, BaseParams, BaseResult
from .base import LayerReport, layer_report, layers_in_tree_order


class GetLayersParams(BaseParams):
    """Which part of the tree's layers to answer with."""

    limit: int | None = Field(
        default=None,
        ge=1,
        description="How many layers to answer with at most; every layer when omitted.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="How many layers to skip, counted in the layer tree's own order.",
    )


class GetLayersResult(BaseResult):
    """The layers asked for, and how many the project holds in all."""

    layers: list[LayerReport] = Field(description="Layers in the layer tree's order.")
    total: int = Field(
        ge=0,
        description="Layers the project holds, however many this answer carries.",
    )


class GetLayers(Action):
    """Return one layer report per layer, in the order the layer tree lists them."""

    name = "get_layers"
    params_model = GetLayersParams

    def run(self, ctx: ActionContext, params: GetLayersParams) -> dict[str, Any]:
        layers = layers_in_tree_order(ctx.project)
        page = (
            layers[params.offset :]
            if params.limit is None
            else layers[params.offset : params.offset + params.limit]
        )
        return GetLayersResult(
            layers=[layer_report(entry.layer, entry.node) for entry in page], total=len(layers)
        ).model_dump(mode="json")
