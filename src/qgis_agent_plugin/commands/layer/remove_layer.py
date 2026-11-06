"""Remove a layer from the project."""

from __future__ import annotations

from pydantic import Field

from ...command import Command, CommandContext, Params, Result
from .report import LayerReport, layer_report
from .shared import require_layer


class RemoveLayerParams(Params):
    """The layer to drop."""

    layer_id: str = Field(min_length=1, max_length=256)


class RemoveLayerResult(Result):
    """What left the project, and two things a caller cannot ask afterwards.

    ``removed`` describes the layer the way ``get_layers`` did one call earlier:
    the registry deletes the layer itself when it lets go of it, so reading it
    here is the last chance to record where the layer came from (``uri`` is what
    ``add_layer`` would take back).

    ``discarded_unsaved_edits`` is true when the layer was in edit mode with
    changes not yet committed: removing a layer writes nothing back, so those
    changes are gone, and QGIS's own window does not ask either.  ``was_required``
    is true when the project had marked the layer as required -- the flag QGIS's
    window locks such a layer with -- which this command removes anyway, because
    a command that took the lock as a refusal would leave a caller with no way to
    unlock it.
    """

    removed: LayerReport
    layer_count: int = Field(ge=0, description="Layers the project still holds.")
    discarded_unsaved_edits: bool = Field(
        description="The layer was in edit mode with uncommitted changes, and removing it "
        "discarded them: removing a layer writes nothing back."
    )
    was_required: bool = Field(
        description="The project had marked this layer as required (QgsMapLayer.LayerFlag"
        ".Removable cleared); it was removed anyway."
    )


class RemoveLayer(Command):
    """Drop one layer from the current project."""

    name = "remove_layer"
    params_model = RemoveLayerParams

    def run(self, ctx: CommandContext, params: RemoveLayerParams) -> RemoveLayerResult:
        from qgis.core import QgsMapLayer, QgsVectorLayer

        # Read the layer before dropping it: the registry deletes the layer itself
        # when it lets go of it, so every fact about it has to be read here.
        layer = require_layer(ctx.project, params.layer_id)
        result = RemoveLayerResult(
            removed=layer_report(layer),
            layer_count=len(ctx.project.mapLayers()) - 1,
            discarded_unsaved_edits=(
                isinstance(layer, QgsVectorLayer) and layer.isEditable() and layer.isModified()
            ),
            was_required=not (layer.flags() & QgsMapLayer.LayerFlag.Removable),
        )
        ctx.project.removeMapLayer(params.layer_id)

        return result
