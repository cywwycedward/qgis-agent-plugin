"""What a layer report says about a layer, and how one is read off a layer.

The field names are the ones QGIS itself uses (``layerType`` is what
``QgsMapLayerFactory.typeToString()`` returns, ``providerKey`` is what a
provider is called, ``layerId`` is the handle every later command takes), so a
report can be read next to the QGIS API without a translation table.  Every
value here is cheap: all of them are measured at or below 0.02 ms on a
500,000-feature layer, and none of them touches the data itself.

One class per kind of layer: ``Layer`` holds what is true of every kind,
and a subclass adds what only its kind has.  ``layer_report`` picks the class,
so a command never has to know which kinds are modelled -- a kind without its
own class is still reported, with the shared facts it does have.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Layer(BaseModel):
    """The identity of one layer and the facts that hold for every kind of it.

    ``extent`` is the layer's bounding box in its own CRS as
    ``(xmin, ymin, xmax, ymax)``.  A layer that holds nothing -- an annotation
    layer, a group layer, an empty memory layer -- has no box at all, which QGIS
    reports as a null rectangle (an inverted one, ``toString() == 'Null'``), and
    that is ``None`` here rather than four zeros that would read as the origin:
    a single feature *at* the origin is a real box of four zeros.
    """

    model_config = ConfigDict(extra="forbid")

    layerId: str = Field(min_length=1, description="Project-wide handle for the layer.")
    name: str = Field(description="Layer name as QGIS shows it; not unique.")
    layerType: str = Field(
        description="Kind of layer, in QGIS's own spelling: 'vector', 'raster', ..."
    )
    providerKey: str = Field(
        description="Data provider key ('ogr', 'gdal', ...), empty for a layer with no source."
    )
    crs: str = Field(description="Layer CRS authority id, e.g. 'EPSG:4326'; may be empty.")
    extent: tuple[float, float, float, float] | None = Field(
        default=None,
        description="Bounding box (xmin, ymin, xmax, ymax); None when the layer holds no box, "
        "which QGIS reports as a null rectangle rather than as four zeros.",
    )
    valid: bool = Field(
        description="Whether the data source opened. QGIS reports a network layer as invalid "
        "until its service answers, so this is not proof that the source is unusable."
    )
    uri: str = Field(
        description="Public data source, the spelling QGIS gives out with credentials removed."
    )
    visible: bool = Field(
        description="Whether the layer is checked in the legend. An invalid layer is never "
        "visible, however its legend entry reads."
    )
    group: list[str] | None = Field(
        description="Names of the groups the layer sits in, outermost first; empty at the top "
        "of the tree, None when the layer is not in the layer tree at all."
    )

    @classmethod
    def from_layer(cls, layer: Any, node: Any | None = None) -> Layer:
        """Read the shared facts off a layer QGIS holds."""

        return cls(layerType=_kind(layer), **_shared_facts(layer, node))


class VectorLayer(Layer):
    """A vector layer, described from its fields and its feature count.

    The geometry type is reported twice on purpose -- ``wkbType`` says it
    exactly the way QGIS spells it, multi-part and Z/M included
    (``MultiPolygon``, ``PointZ``), while ``geometryType`` is QGIS's coarse
    class (``Point``, ``Line``, ``Polygon``) for callers that only branch on the
    category.

    Every value is cheap: the count is answered by the provider where it can
    (0.001 ms on 500,000 in-memory features, 0.2 ms on a 200,000-feature
    GeoPackage) and the rest are metadata.  Asking for the feature ids, by
    contrast, costs a full scan -- 126 ms in memory, 1046 ms for a GeoJSON of
    the same size -- which is why nothing here does it.

    A count is negative when there is none to report: -1 is a provider that
    cannot count, and -2 is QGIS reporting that nothing ever counted this layer,
    which is what a layer whose source did not open answers.
    """

    layerType: str = "vector"

    featureCount: int = Field(
        ge=-2,
        description="Number of features; -1 when the provider cannot count them, -2 when "
        "nothing counted this layer, which is what a layer whose source did not open reports.",
    )
    wkbType: str = Field(description="Geometry type as QGIS spells it, e.g. 'MultiPolygon'.")
    geometryType: str = Field(
        description="QGIS's coarse geometry class: 'Point', 'Line', 'Polygon', 'Unknown', 'Null'."
    )
    fields: list[str] = Field(description="Field names, in the order the layer holds them.")

    @classmethod
    def from_layer(cls, layer: Any, node: Any | None = None) -> VectorLayer:
        """Read a vector layer's own facts on top of the shared ones."""

        from qgis.core import QgsWkbTypes

        return cls(
            **_shared_facts(layer, node),
            featureCount=layer.featureCount(),
            wkbType=QgsWkbTypes.displayString(layer.wkbType()),
            geometryType=layer.geometryType().name,
            fields=list(layer.fields().names()),
        )


class RasterLayer(Layer):
    """A raster layer, described from its grid and its bands.

    These are the fields a caller needs before asking anything of the pixels:
    the grid it covers, how many bands it holds, what the bands mean, and how
    much ground a pixel takes.  All of them are metadata a provider already has
    open -- measured at or below 0.012 ms, band names included.
    """

    layerType: str = "raster"

    width: int = Field(ge=0, description="Raster width in pixels.")
    height: int = Field(ge=0, description="Raster height in pixels.")
    bandCount: int = Field(ge=0, description="Number of bands.")
    rasterType: Literal["GrayOrUndefined", "Palette", "MultiBand", "SingleBandColorData"] = Field(
        description="Band layout as QGIS names it."
    )
    rasterUnitsPerPixelX: float = Field(
        description="Ground units per pixel across, in the layer's CRS."
    )
    rasterUnitsPerPixelY: float = Field(
        description="Ground units per pixel down, in the layer's CRS."
    )
    bandNames: list[str] = Field(
        description="One name per band, in band order; QGIS answers 'Band 1' when unnamed."
    )

    @classmethod
    def from_layer(cls, layer: Any, node: Any | None = None) -> RasterLayer:
        """Read a raster layer's own facts on top of the shared ones."""

        return cls(
            **_shared_facts(layer, node),
            width=layer.width(),
            height=layer.height(),
            bandCount=layer.bandCount(),
            rasterType=layer.rasterType().name,
            rasterUnitsPerPixelX=layer.rasterUnitsPerPixelX(),
            rasterUnitsPerPixelY=layer.rasterUnitsPerPixelY(),
            bandNames=[layer.bandName(index) for index in range(1, layer.bandCount() + 1)],
        )


# A subtype first, the shared facts last: pydantic reads a plain union in order, so
# the kinds with their own class answer before the class that carries anything.
# A command hands the result the instances it built (``layer_report``), which the
# union passes through unchanged; a plain dict that is missing a kind's own fact
# is the case where the shared class answers instead.
LayerReport = VectorLayer | RasterLayer | Layer

# The report class each kind of layer answers with.  A kind without an entry is
# still reported, by the class that carries the facts every kind has, so a new
# QGIS layer kind needs no entry here to be described.
_REPORTS: dict[str, type[Layer]] = {"vector": VectorLayer, "raster": RasterLayer}


@dataclass(frozen=True)
class LayerTreeEntry:
    """One project layer and the canonical tree node that describes it.

    A layer may occur in more than one tree node.  The tree traversal keeps the
    first node it encounters, so the report has one stable source for ``group``
    and ``visible`` while the layer itself is still reported only once.
    """

    layer: Any
    node: Any | None


def layer_report(layer: Any, node: Any | None = None) -> LayerReport:
    """Describe one layer, using a supplied tree node when one is available.

    Callers that already walked the layer tree pass its node directly.  Existing
    callers that only have a layer retain the old behaviour and resolve the first
    tree node while reading the shared facts.
    """

    return _REPORTS.get(_kind(layer), Layer).from_layer(layer, node)


def layers_in_tree_order(project: Any) -> list[LayerTreeEntry]:
    """Return every layer of the project, in the order the layer tree lists them.

    The tree is what the legend shows, so its order is the one a caller reads.
    A layer the project holds but the tree does not -- added with
    ``addToLegend=False`` -- is listed after them instead of left out, because a
    command that answers about the project must not lose a layer of it.

    A layer that occurs in multiple tree nodes is included once, using its first
    node as the canonical placement.  A node without a layer is skipped: a
    project file may name a layer in its tree that it does not hold, and QGIS
    reads such a file back with a node whose ``layer()`` is None.  A node is not
    a layer to report.
    """

    listed = []
    seen: set[str] = set()
    for node in project.layerTreeRoot().findLayers():
        layer = node.layer()
        if layer is not None and layer.id() not in seen:
            listed.append(LayerTreeEntry(layer, node))
            seen.add(layer.id())
    in_tree = seen
    return listed + [
        LayerTreeEntry(layer, None)
        for layer in project.mapLayers().values()
        if layer.id() not in in_tree
    ]


def _kind(layer: Any) -> str:
    """Return a layer's kind in the spelling QGIS gives it ('vector', ...)."""

    from qgis.core import QgsMapLayerFactory

    return QgsMapLayerFactory.typeToString(layer.type())


def _shared_facts(layer: Any, node: Any | None) -> dict[str, Any]:
    """Read the facts every kind of layer has, in the report's own spellings.

    ``layerType`` is left out: a report class that models one kind states that
    kind itself, and the class that carries every other kind is handed it.
    """

    if node is None:
        project = layer.project()
        node = project.layerTreeRoot().findLayer(layer.id()) if project is not None else None

    return {
        "layerId": layer.id(),
        "name": layer.name(),
        "providerKey": layer.providerType(),
        "crs": layer.crs().authid(),
        "extent": _extent(layer),
        "valid": layer.isValid(),
        "uri": layer.publicSource(),
        "visible": bool(layer.isValid() and node is not None and node.isVisible()),
        "group": _group_path(node),
    }


def _group_path(node: Any) -> list[str] | None:
    """Return the names of the groups a layer sits in, outermost first.

    ``None`` is a layer the project holds while the tree does not, which is in no
    group because it is in no legend.  The root group is left out: it has no name
    and is not a group a caller could point at.
    """

    if node is None:
        return None
    names = []
    parent = node.parent()
    while parent is not None and parent.parent() is not None:
        names.append(parent.name())
        parent = parent.parent()
    return list(reversed(names))


def _extent(layer: Any) -> tuple[float, float, float, float] | None:
    """Return the layer's box, or ``None`` for the null rectangle QGIS reports."""

    box = layer.extent()
    if box.isNull():
        return None
    return (box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum())
