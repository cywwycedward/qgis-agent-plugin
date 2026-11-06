"""List the project's layers, in the order the layer tree lists them.

The order is the layer tree's -- what the legend shows -- so reading the answer
top-down is reading the stack the canvas draws.  A project can override that
order for the canvas alone (``hasCustomLayerOrder``); this command answers in the
tree's order either way, because the tree is what both the caller and the legend
look at.
"""

from __future__ import annotations

from pydantic import Field

from ...command import Command, CommandContext, Params, Result
from .report import LayerReport, layer_report, layers_in_tree_order


class GetLayersParams(Params):
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


class GetLayersResult(Result):
    """The layers asked for, and how many the project holds in all."""

    layers: list[LayerReport] = Field(description="Layers in the layer tree's order.")
    total: int = Field(
        ge=0,
        description="Layers the project holds, however many this answer carries.",
    )


class GetLayers(Command):
    """Return one layer report per layer, in the order the layer tree lists them."""

    name = "get_layers"
    params_model = GetLayersParams

    def run(self, ctx: CommandContext, params: GetLayersParams) -> GetLayersResult:
        layers = layers_in_tree_order(ctx.project)
        page = (
            layers[params.offset :]
            if params.limit is None
            else layers[params.offset : params.offset + params.limit]
        )
        return GetLayersResult(
            layers=[layer_report(entry.layer, entry.node) for entry in page], total=len(layers)
        )
