"""Read features from a vector layer, bounded by the caller's limit.

Not in use for now: this command is not in ``qgis_actions/registry.py`` and has no
MCP tool, so nothing can reach it.  It is kept for the redesign of
``get_layer_features`` -- which fields, which geometry, what a page of features
says about the ones it left out -- and its tests went with it, because they drove
the socket, which is exactly what a command outside the surface cannot do.
Restore this from git history if it is ever put back unredesigned.
"""

from __future__ import annotations

import base64
from typing import Any

from pydantic import Field, StrictInt

from ..base import Action, ActionContext, ActionError, BaseParams
from ..utils import require_layer


class GetLayerFeaturesParams(BaseParams):
    """The vector layer to read and how many features to return."""

    layer_id: str = Field(min_length=1, max_length=256)
    limit: StrictInt = Field(default=10, ge=1)


def _attribute_value(value: Any) -> Any:
    """Return one attribute as something JSON can carry.

    QDate/QDateTime/QTime and bytes have no JSON form, so they leave as an ISO
    string and base64; anything else unknown leaves as text, so a value this
    helper does not know cannot drop the connection.
    """

    from qgis.PyQt.QtCore import QDate, QDateTime, Qt, QTime

    if isinstance(value, (QDate, QTime, QDateTime)):
        return value.toString(Qt.ISODate)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


class GetLayerFeatures(Action):
    """Return at most ``limit`` features with attributes and WKT geometry."""

    name = "get_layer_features"
    params_model = GetLayerFeaturesParams

    def run(self, ctx: ActionContext, params: GetLayerFeaturesParams) -> dict[str, Any]:
        from qgis.core import QgsMapLayer

        layer_id = params.layer_id
        layer = require_layer(ctx.project, layer_id)
        if layer.type() != QgsMapLayer.VectorLayer:
            raise ActionError("layer_not_vector", f"Layer is not a vector layer: {layer_id}")

        fields = [field.name() for field in layer.fields()]
        features = []
        for index, feature in enumerate(layer.getFeatures()):
            if index >= params.limit:
                break
            geometry = None
            if feature.hasGeometry():
                geometry = {
                    "type": feature.geometry().type(),
                    "wkt": feature.geometry().asWkt(precision=4),
                }
            features.append(
                {
                    "id": feature.id(),
                    "attributes": {
                        field.name(): _attribute_value(feature.attribute(field.name()))
                        for field in layer.fields()
                    },
                    "geometry": geometry,
                }
            )
        return {
            "layer_id": layer_id,
            "feature_count": layer.featureCount(),
            "features": features,
            "fields": fields,
        }
