"""Move the map view to one layer's extent."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ..base import Action, ActionContext, ActionError, BaseParams, BaseResult
from ..utils import require_layer


class ZoomToLayerParams(BaseParams):
    """The layer to move the view to."""

    layer_id: str = Field(min_length=1, max_length=256)


class ZoomToLayerResult(BaseResult):
    """The view the zoom left behind, read off the canvas.

    ``extent`` is the visible box in ``crs``.  It is not the layer's own box:
    a canvas fits what it is given to its window, so the view is the layer's box
    grown to the window's shape -- a square layer on a wide window reads back
    wider than it is, and the two boxes share a centre.  ``scale`` is what the
    canvas reports for that view, so a caller can tell a zoom from a pan.

    ``layer_crs`` is the layer's own CRS, and it is empty when the source
    declares none: QGIS then reads the layer's coordinates as if they were the
    canvas CRS, so the view is somewhere real only if the caller knows what
    those numbers meant.
    """

    zoomed_to: str = Field(description="Id of the layer the view was moved to.")
    extent: tuple[float, float, float, float] = Field(
        description="Visible box (xmin, ymin, xmax, ymax) in ``crs``, as the canvas fitted it."
    )
    scale: float = Field(description="Scale of that view, as the canvas reports it.")
    crs: str = Field(description="CRS the extent is in: the canvas's own, not the layer's.")
    layer_crs: str = Field(description="The layer's own CRS, empty when its source declares none.")


class ZoomToLayer(Action):
    """Move the map view to the named layer's extent.

    The view only: the layer is not made current, and the project is left as it
    was -- no layer is added, removed or restyled, and nothing is written.
    """

    name = "zoom_to_layer"
    params_model = ZoomToLayerParams

    def run(self, ctx: ActionContext, params: ZoomToLayerParams) -> dict[str, Any]:
        from qgis.core import QgsCsException, QgsReferencedRectangle

        layer = require_layer(ctx.project, params.layer_id)
        canvas = ctx.iface.mapCanvas() if ctx.iface is not None else None
        if canvas is None:
            raise ActionError(
                "no_canvas",
                "This QGIS has no map canvas to move: there is no window to show a view in.",
            )

        box = layer.extent()
        if box.isNull():
            raise ActionError(
                "layer_has_no_extent",
                f"{layer.name()!r} has no extent to move to: the layer holds nothing, or its "
                "kind carries no box at all (an annotation or group layer).",
            )

        # The canvas does the CRS work: it transforms the box into its own CRS,
        # ballpark transforms allowed, and answers false when nothing is left of
        # it.  A source without a CRS is not an error there -- QGIS reads the
        # numbers as canvas coordinates -- which is why the answer says which CRS
        # the layer had.
        try:
            moved = canvas.setReferencedExtent(QgsReferencedRectangle(box, layer.crs()))
        except QgsCsException as error:
            raise ActionError("crs_transform_failed", str(error)) from error
        if not moved:
            raise ActionError(
                "crs_transform_failed",
                f"{layer.name()!r} cannot be brought into the canvas CRS "
                f"{canvas.mapSettings().destinationCrs().authid() or '(none)'}.",
            )
        canvas.refresh()

        view = canvas.extent()
        return ZoomToLayerResult(
            zoomed_to=params.layer_id,
            extent=(view.xMinimum(), view.yMinimum(), view.xMaximum(), view.yMaximum()),
            scale=canvas.scale(),
            crs=canvas.mapSettings().destinationCrs().authid(),
            layer_crs=layer.crs().authid(),
        ).model_dump(mode="json")
