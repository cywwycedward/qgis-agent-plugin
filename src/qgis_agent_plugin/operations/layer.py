"""The layer operations: load a local file, list and inspect layers, toggle visibility.

Every operation here but ``layer.sum_numeric_field`` runs on the main thread and
works through ``ctx.project``, so it reads the project the engine holds rather than
a global singleton.  A QGIS layer never leaves the method that built it: only its
id, name, provider, CRS and the two visibility flags travel out, because those are
the values a caller can act on and the only ones that survive serialisation.

The field sum is the one background operation, so its reading is split across the
three stages: the numbers are copied out on the main thread, summed off it over a
tuple of plain floats, and checked against the project generation on the way back
(PRD 6.2, 7.10).

``visible`` and ``effective_visible`` are deliberately two fields.  A layer's own
check state can be true while its parent group hides it, and reporting one number
for both would make a toggle look like it failed (PRD 7.7, AT-21B).

The style reads (``layer.list_style_categories``, ``layer.get_qml``,
``layer.list_available_renderers``, ``layer.get_renderer``) describe what a layer
already carries without changing it.  Which categories apply is the PRD's fixed
list, not something read back out of the XML; the QML itself comes from the
layer's own native export; and both renderer lists come from the live QGIS
registries rather than a table kept here.

The renderer setter is the one operation here that replaces state.  It builds the
whole native renderer before the layer is touched, then installs it in one call,
so a refused configuration cannot leave the layer with half a style or with none
(PRD v2 5.4).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from ..core.contracts import (
    BackgroundOperationMixin,
    BaseCommand,
    BaseQuery,
    MainThreadOperationMixin,
    OperationContext,
    TaskControl,
)
from ..core.models import (
    INTERNAL_ERROR,
    INVALID_FIELD_TYPE,
    INVALID_INPUT,
    INVALID_LAYER,
    LAYER_NOT_FOUND,
    LAYER_NOT_IN_TREE,
    SNAPSHOT_LIMIT_EXCEEDED,
    STALE_PROJECT,
    FrameworkError,
    Params,
)
from ..core.security import resolve_path

# What a caller may ask to load; ``other`` exists only on the reading side.
LayerKind = Literal["vector", "raster"]
LayerSummaryKind = Literal["vector", "raster", "other"]


class LayerAddParams(Params):
    """One local file to load, under a name the caller chooses (PRD 7.6)."""

    kind: LayerKind
    source_path: StrictStr = Field(min_length=1)
    name: StrictStr = Field(min_length=1, max_length=128)
    sub_layer: StrictStr | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    add_to_legend: StrictBool = True

    @model_validator(mode="after")
    def _sub_layer_is_vector_only(self) -> LayerAddParams:
        if self.sub_layer is not None and self.kind != "vector":
            raise ValueError("sub_layer is only valid for a vector source")
        return self


class LayerAddResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: StrictStr
    name: StrictStr
    kind: LayerKind
    provider: StrictStr
    crs_authid: StrictStr


class LayerSummary(BaseModel):
    """One registered layer in the terms a caller can act on (PRD 7.7)."""

    model_config = ConfigDict(extra="forbid")

    layer_id: StrictStr
    name: StrictStr
    kind: LayerSummaryKind
    crs_authid: StrictStr
    in_layer_tree: StrictBool
    visible: StrictBool | None
    effective_visible: StrictBool | None


class LayerListParams(Params):
    pass


class LayerListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layers: list[LayerSummary]


class LayerGetInfoParams(Params):
    layer_id: StrictStr = Field(min_length=1)


class LayerGetInfoResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: StrictStr
    name: StrictStr
    kind: LayerSummaryKind
    provider: StrictStr
    crs_authid: StrictStr
    valid: StrictBool
    in_layer_tree: StrictBool
    visible: StrictBool | None
    effective_visible: StrictBool | None


class LayerSetVisibilityParams(Params):
    layer_id: StrictStr = Field(min_length=1)
    visible: StrictBool


class LayerSetVisibilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: StrictStr
    visible: StrictBool
    effective_visible: StrictBool
    changed: StrictBool


def layer_source_uri(source: Path, sub_layer: str | None) -> str:
    """Return the provider URI for an already-validated local file.

    The path has been through ``resolve_path``, so it is a real local file and no
    untrusted string is being assembled here.  A sub-layer still needs the ogr
    provider's own encoder: how its URI joins a path and a layer name, and how it
    escapes each part, is the provider's rule -- re-deriving the
    ``path|layername=name`` shape by hand is a guess about that rule.

    shortcut: this vets the file the caller named, not the sources that file
    itself names -- a local ``.vrt`` may carry a ``/vsicurl/`` or WMS source
    inside it; upgrade to reading a container's own sources if untrusted VRTs
    become an input.
    """

    if sub_layer is None:
        return str(source)
    from qgis.core import QgsProviderRegistry

    return (
        QgsProviderRegistry.instance()
        .providerMetadata("ogr")
        .encodeUri({"path": str(source), "layerName": sub_layer})
    )


def layer_kind(layer: Any) -> LayerSummaryKind:
    """Classify a registered layer; anything QGIS can hold is listed, not refused."""

    from qgis.core import QgsRasterLayer, QgsVectorLayer

    if isinstance(layer, QgsVectorLayer):
        return "vector"
    if isinstance(layer, QgsRasterLayer):
        return "raster"
    return "other"


def layer_tree_node(project: Any, layer_id: str) -> Any | None:
    """The layer's node in the current project's tree, or ``None`` if it has none."""

    return project.layerTreeRoot().findLayer(layer_id)


def layer_visibility(project: Any, layer_id: str) -> tuple[bool, bool | None, bool | None]:
    """``(in_layer_tree, visible, effective_visible)`` for one layer.

    A layer with no node has no visibility of its own, so both flags are ``None``
    rather than a false claim that it is hidden (PRD 7.7).
    """

    node = layer_tree_node(project, layer_id)
    if node is None:
        return False, None, None
    return True, bool(node.itemVisibilityChecked()), bool(node.isVisible())


def layer_summary(project: Any, layer_id: str, layer: Any) -> LayerSummary:
    """The one place a summary is built, so list and get_info cannot disagree."""

    in_layer_tree, visible, effective_visible = layer_visibility(project, layer_id)
    return LayerSummary(
        layer_id=layer_id,
        name=layer.name(),
        kind=layer_kind(layer),
        crs_authid=layer.crs().authid(),
        in_layer_tree=in_layer_tree,
        visible=visible,
        effective_visible=effective_visible,
    )


class LayerAdd(
    BaseCommand[LayerAddParams, LayerAddResult],
    MainThreadOperationMixin[LayerAddParams, LayerAddResult],
):
    """Load a local vector or raster file into the current project."""

    name = "layer.add"
    description = "Add a local vector or raster file to the project"
    input_model = LayerAddParams
    output_model = LayerAddResult

    def execute_main(self, ctx: OperationContext, request: LayerAddParams) -> LayerAddResult:
        from qgis.core import QgsRasterLayer, QgsVectorLayer

        source = resolve_path(request.source_path, ctx.policy.allowed_data_roots, must_exist=True)
        # The provider is a function of the declared kind: a caller cannot name one.
        provider = "ogr" if request.kind == "vector" else "gdal"
        layer_class = QgsVectorLayer if request.kind == "vector" else QgsRasterLayer
        layer = layer_class(layer_source_uri(source, request.sub_layer), request.name, provider)
        if not layer.isValid():
            raise FrameworkError(INVALID_LAYER, "the layer source could not be loaded")
        ctx.project.addMapLayer(layer, request.add_to_legend)
        return LayerAddResult(
            layer_id=layer.id(),
            name=layer.name(),
            kind=request.kind,
            provider=provider,
            crs_authid=layer.crs().authid(),
        )


class LayerList(
    BaseQuery[LayerListParams, LayerListResult],
    MainThreadOperationMixin[LayerListParams, LayerListResult],
):
    """Every registered layer, sorted by id so the output is stable."""

    name = "layer.list"
    description = "List the layers registered in the project"
    input_model = LayerListParams
    output_model = LayerListResult

    def execute_main(self, ctx: OperationContext, request: LayerListParams) -> LayerListResult:
        layers = [
            layer_summary(ctx.project, layer_id, layer)
            for layer_id, layer in ctx.project.mapLayers().items()
        ]
        layers.sort(key=lambda summary: summary.layer_id)
        return LayerListResult(layers=layers)


class LayerGetInfo(
    BaseQuery[LayerGetInfoParams, LayerGetInfoResult],
    MainThreadOperationMixin[LayerGetInfoParams, LayerGetInfoResult],
):
    """One layer's identity and state, including whether its source still loads."""

    name = "layer.get_info"
    description = "Read one layer's identity, validity and visibility"
    input_model = LayerGetInfoParams
    output_model = LayerGetInfoResult

    def execute_main(
        self, ctx: OperationContext, request: LayerGetInfoParams
    ) -> LayerGetInfoResult:
        layer = project_layer(ctx.project, request.layer_id)
        summary = layer_summary(ctx.project, request.layer_id, layer)
        return LayerGetInfoResult(
            **summary.model_dump(),
            provider=layer.providerType(),
            valid=layer.isValid(),
        )


class LayerSetVisibility(
    BaseCommand[LayerSetVisibilityParams, LayerSetVisibilityResult],
    MainThreadOperationMixin[LayerSetVisibilityParams, LayerSetVisibilityResult],
):
    """Toggle one layer's own check state; a parent group stays as it is."""

    name = "layer.set_visibility"
    description = "Check or uncheck one layer in the layer tree"
    input_model = LayerSetVisibilityParams
    output_model = LayerSetVisibilityResult

    def execute_main(
        self, ctx: OperationContext, request: LayerSetVisibilityParams
    ) -> LayerSetVisibilityResult:
        project_layer(ctx.project, request.layer_id)
        node = layer_tree_node(ctx.project, request.layer_id)
        if node is None:
            raise FrameworkError(LAYER_NOT_IN_TREE, "layer has no layer tree node")
        changed = bool(node.itemVisibilityChecked()) != request.visible
        if changed:
            node.setItemVisibilityChecked(request.visible)
        return LayerSetVisibilityResult(
            layer_id=request.layer_id,
            visible=bool(node.itemVisibilityChecked()),
            effective_visible=bool(node.isVisible()),
            changed=changed,
        )


class SumNumericFieldParams(Params):
    layer_id: StrictStr = Field(min_length=1)
    field_name: StrictStr = Field(min_length=1)


class SumNumericFieldResult(BaseModel):
    """The sum of a snapshot, which is not the layer's value when the task ends.

    shortcut: the values are summed as float64, so a QInt64 field with values above
    2**53 loses precision in ``total``; add an integer path when an exact integer
    sum is needed.
    """

    model_config = ConfigDict(extra="forbid")

    layer_id: StrictStr
    field_name: StrictStr
    count: int
    skipped_null: int
    total: float
    snapshot_project_epoch: int


@dataclass(frozen=True)
class FieldValues:
    """A copied-out field: plain floats, so no QGIS object reaches the worker."""

    layer_id: str
    field_name: str
    values: tuple[float, ...]
    skipped_null: int
    project_epoch: int


@dataclass(frozen=True)
class FieldSum:
    """What the worker computed, with the generation it was computed from."""

    count: int
    total: float
    skipped_null: int
    project_epoch: int


class LayerSumNumericField(
    BaseQuery[SumNumericFieldParams, SumNumericFieldResult],
    BackgroundOperationMixin[SumNumericFieldParams, SumNumericFieldResult, FieldValues, FieldSum],
):
    """Sum one numeric field off the main thread, over a snapshot (PRD 7.10).

    The task declares ``dependent_layer_ids`` so the adapter can hand it to
    ``QgsTask.setDependentLayers``: removing the layer then cancels the task, as
    QGIS does for any dependent task (PRD 8.4).
    """

    name = "layer.sum_numeric_field"
    description = "Sum one numeric field of a vector layer over a copied snapshot"
    input_model = SumNumericFieldParams
    output_model = SumNumericFieldResult

    def prepare_main(self, ctx: OperationContext, request: SumNumericFieldParams) -> FieldValues:
        from qgis.core import NULL, QgsVectorLayer

        layer = project_layer(ctx.project, request.layer_id)
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            raise FrameworkError(INVALID_LAYER, "the layer is not a valid vector layer")
        fields = layer.fields()
        index = fields.indexFromName(request.field_name)
        if index < 0:
            raise FrameworkError(INVALID_FIELD_TYPE, "the layer has no such field")
        if not fields.at(index).isNumeric():
            raise FrameworkError(INVALID_FIELD_TYPE, "the field is not integer or floating point")

        limit = ctx.policy.max_snapshot_features
        values: list[float] = []
        skipped = 0
        for seen, feature in enumerate(layer.getFeatures(), start=1):
            if seen > limit:
                # Never truncate quietly: a partial sum that looks complete is worse
                # than a refusal (PRD 7.10).
                raise FrameworkError(
                    SNAPSHOT_LIMIT_EXCEEDED, f"the layer has more than {limit} features"
                )
            value = feature[index]
            # QGIS hands a null attribute back as its own NULL singleton (a QVariant),
            # and some builds return None instead; both are the "skipped null" case.
            if value is None or value == NULL:
                skipped += 1
                continue
            number = float(value)
            if not math.isfinite(number):
                # A record whose ``total`` is NaN or Infinity is not JSON an RFC 8259
                # parser reads (measured: json.dumps(..., allow_nan=False) raises), so
                # the sum refuses rather than deliver a result the caller cannot parse.
                raise FrameworkError(
                    INVALID_FIELD_TYPE, "the field holds a non-finite (NaN or infinite) value"
                )
            values.append(number)
        return FieldValues(
            layer_id=request.layer_id,
            field_name=request.field_name,
            values=tuple(values),
            skipped_null=skipped,
            project_epoch=ctx.lifecycle.current_epoch,
        )

    def execute_worker(self, snapshot: FieldValues, control: TaskControl) -> FieldSum:
        values = snapshot.values
        step = max(1, len(values) // 100)
        total = 0.0
        count = 0
        for position, value in enumerate(values, start=1):
            if position % step == 0:
                if control.is_cancelled():
                    # Stopping here is the cooperative half of the cancel; the adapter
                    # turns the partial run into CANCELLED and drops the numbers.
                    break
                control.set_progress(100.0 * position / len(values))
            total += value
            count += 1
        if not math.isfinite(total):
            # Two finite values can still add up to Infinity (1e308 + 1e308), and a
            # record whose ``total`` is Infinity is not JSON an RFC 8259 parser reads
            # (measured: json.dumps(..., allow_nan=False) raises), so it is refused
            # rather than delivered.
            raise FrameworkError(INVALID_FIELD_TYPE, "the sum of the field is not a finite number")
        return FieldSum(
            count=count,
            total=total,
            skipped_null=snapshot.skipped_null,
            project_epoch=snapshot.project_epoch,
        )

    def finalize_main(
        self, ctx: OperationContext, request: SumNumericFieldParams, result: FieldSum
    ) -> SumNumericFieldResult:
        if ctx.lifecycle.current_epoch != result.project_epoch:
            raise FrameworkError(
                STALE_PROJECT, "the project was replaced while the field was summed"
            )
        return SumNumericFieldResult(
            layer_id=request.layer_id,
            field_name=request.field_name,
            count=result.count,
            skipped_null=result.skipped_null,
            total=result.total,
            snapshot_project_epoch=result.project_epoch,
        )

    def dependent_layer_ids(self, request: SumNumericFieldParams) -> list[str]:
        return [request.layer_id]


# --- reading the style: categories, QML and renderer types -------------------

# The 18 QgsMapLayer.StyleCategory names in the order QGIS declares them, and the
# subset QgsRasterLayer actually exports (PRD v2 4.1-4.2).  They are names here,
# not flags: the bit flag is only built for a request, so this module still loads
# without QGIS.
StyleCategoryName = Literal[
    "LayerConfiguration",
    "Symbology",
    "Symbology3D",
    "Labeling",
    "Fields",
    "Forms",
    "Actions",
    "MapTips",
    "Diagrams",
    "AttributeTable",
    "Rendering",
    "CustomProperties",
    "GeometryOptions",
    "Relations",
    "Temporal",
    "Legend",
    "Elevation",
    "Notes",
]

VECTOR_STYLE_CATEGORIES: tuple[StyleCategoryName, ...] = (
    "LayerConfiguration",
    "Symbology",
    "Symbology3D",
    "Labeling",
    "Fields",
    "Forms",
    "Actions",
    "MapTips",
    "Diagrams",
    "AttributeTable",
    "Rendering",
    "CustomProperties",
    "GeometryOptions",
    "Relations",
    "Temporal",
    "Legend",
    "Elevation",
    "Notes",
)

RASTER_STYLE_CATEGORIES: tuple[StyleCategoryName, ...] = (
    "LayerConfiguration",
    "Symbology",
    "Labeling",
    "MapTips",
    "AttributeTable",
    "Rendering",
    "CustomProperties",
    "Temporal",
    "Elevation",
    "Notes",
)


def project_layer(project: Any, layer_id: str) -> Any:
    """The layer behind an id, or ``LAYER_NOT_FOUND``; every reader starts here."""

    layer = project.mapLayer(layer_id)
    if layer is None:
        raise FrameworkError(LAYER_NOT_FOUND, "layer does not exist")
    return layer


def layer_for_style(project: Any, layer_id: str) -> Any:
    """The layer behind an id, when it is a valid spatial vector or raster.

    The whole read side is defined only for those two shapes.  A vector table, an
    invalid layer or somebody else's layer type is ``INVALID_LAYER`` rather than
    an empty answer, because "no categories" and "not applicable" are different
    results (PRD v2 4.2, 5.2).
    """

    from qgis.core import Qgis

    layer = project_layer(project, layer_id)
    if not layer.isValid():
        raise FrameworkError(INVALID_LAYER, "the layer is not valid")
    kind = layer_kind(layer)
    if kind == "raster":
        return layer
    if kind == "vector" and layer.geometryType() in (
        Qgis.GeometryType.Point,
        Qgis.GeometryType.Line,
        Qgis.GeometryType.Polygon,
    ):
        return layer
    raise FrameworkError(INVALID_LAYER, "the layer is not a spatial vector or raster layer")


def style_categories(layer: Any) -> tuple[StyleCategoryName, ...]:
    """The categories that apply to an already-vetted layer, in PRD order."""

    return RASTER_STYLE_CATEGORIES if layer_kind(layer) == "raster" else VECTOR_STYLE_CATEGORIES


def style_category_flag(names: Sequence[str]) -> Any:
    """The native bit flag naming exactly ``names``, in their given order."""

    from qgis.core import QgsMapLayer

    flag = getattr(QgsMapLayer, names[0])
    for name in names[1:]:
        flag = flag | getattr(QgsMapLayer, name)
    return flag


class LayerListStyleCategoriesParams(Params):
    layer_id: StrictStr = Field(min_length=1)


class LayerListStyleCategoriesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: StrictStr
    categories: list[StyleCategoryName]


class LayerGetQmlParams(Params):
    layer_id: StrictStr = Field(min_length=1)
    categories: Annotated[list[StyleCategoryName] | None, Field(min_length=1)] = None


class LayerGetQmlResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: StrictStr
    qml: StrictStr


class LayerListAvailableRenderersParams(Params):
    layer_id: StrictStr = Field(min_length=1)


class LayerListAvailableRenderersResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    renderer_types: list[StrictStr]


class LayerGetRendererParams(Params):
    layer_id: StrictStr = Field(min_length=1)


class LayerGetRendererResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: StrictStr
    renderer_type: StrictStr


class LayerListStyleCategories(
    BaseQuery[LayerListStyleCategoriesParams, LayerListStyleCategoriesResult],
    MainThreadOperationMixin[LayerListStyleCategoriesParams, LayerListStyleCategoriesResult],
):
    """The QML categories this layer type has, whether or not they are populated."""

    name = "layer.list_style_categories"
    description = "List the QML style categories that apply to one layer"
    input_model = LayerListStyleCategoriesParams
    output_model = LayerListStyleCategoriesResult

    def execute_main(
        self, ctx: OperationContext, request: LayerListStyleCategoriesParams
    ) -> LayerListStyleCategoriesResult:
        layer = layer_for_style(ctx.project, request.layer_id)
        return LayerListStyleCategoriesResult(
            layer_id=request.layer_id,
            categories=list(style_categories(layer)),
        )


class LayerGetQml(
    BaseQuery[LayerGetQmlParams, LayerGetQmlResult],
    MainThreadOperationMixin[LayerGetQmlParams, LayerGetQmlResult],
):
    """One layer's style as the QML the layer itself exports, in memory only."""

    name = "layer.get_qml"
    description = "Export one layer's current style as QML XML in memory"
    input_model = LayerGetQmlParams
    output_model = LayerGetQmlResult

    def execute_main(self, ctx: OperationContext, request: LayerGetQmlParams) -> LayerGetQmlResult:
        from qgis.core import QgsReadWriteContext
        from qgis.PyQt.QtXml import QDomDocument

        layer = layer_for_style(ctx.project, request.layer_id)
        applicable = style_categories(layer)
        if request.categories is None:
            chosen = applicable
        else:
            chosen = tuple(request.categories)
            if len(set(chosen)) != len(chosen):
                raise FrameworkError(INVALID_INPUT, "categories must not repeat")
            if any(name not in applicable for name in chosen):
                raise FrameworkError(INVALID_INPUT, "a category does not apply to this layer")

        document = QDomDocument()
        try:
            # 3.44's binding returns the export's error message (empty on success)
            # and fills the document; it does not return (message, ok) as PRD v2 4.3
            # says -- checked against this install's QgsMapLayer.exportNamedStyle.
            message = layer.exportNamedStyle(
                document, QgsReadWriteContext(), style_category_flag(chosen)
            )
        except Exception as error:  # noqa: BLE001 - any exporter failure is one refusal
            raise FrameworkError(INTERNAL_ERROR, "the layer style could not be exported") from error
        if message:
            # The native message may name a file, so it stays out of the record.
            raise FrameworkError(INTERNAL_ERROR, "the layer style could not be exported")
        return LayerGetQmlResult(layer_id=request.layer_id, qml=document.toString())


class LayerListAvailableRenderers(
    BaseQuery[LayerListAvailableRenderersParams, LayerListAvailableRenderersResult],
    MainThreadOperationMixin[LayerListAvailableRenderersParams, LayerListAvailableRenderersResult],
):
    """The renderer type names the live QGIS registries offer for this layer."""

    name = "layer.list_available_renderers"
    description = "List the renderer type names the QGIS registries offer for one layer"
    input_model = LayerListAvailableRenderersParams
    output_model = LayerListAvailableRenderersResult

    def execute_main(
        self, ctx: OperationContext, request: LayerListAvailableRenderersParams
    ) -> LayerListAvailableRenderersResult:
        from qgis.core import QgsApplication, QgsRasterRendererRegistry

        layer = layer_for_style(ctx.project, request.layer_id)
        if layer_kind(layer) == "raster":
            # QgsApplication.rasterRendererRegistry() is SIP_SKIP in 3.44 and the
            # class does not bind instance(), so a registry object is the only
            # Python path to the raster list (PRD v2 5.2 names the accessor that
            # this build does not expose).
            renderer_types = QgsRasterRendererRegistry().renderersList()
        else:
            renderer_types = QgsApplication.rendererRegistry().renderersList(layer)
        return LayerListAvailableRenderersResult(renderer_types=sorted(set(renderer_types)))


class LayerGetRenderer(
    BaseQuery[LayerGetRendererParams, LayerGetRendererResult],
    MainThreadOperationMixin[LayerGetRendererParams, LayerGetRendererResult],
):
    """The type name of the renderer the layer currently has, if any."""

    name = "layer.get_renderer"
    description = "Read the type name of one layer's current renderer"
    input_model = LayerGetRendererParams
    output_model = LayerGetRendererResult

    def execute_main(
        self, ctx: OperationContext, request: LayerGetRendererParams
    ) -> LayerGetRendererResult:
        layer = layer_for_style(ctx.project, request.layer_id)
        renderer = layer.renderer()
        return LayerGetRendererResult(
            layer_id=request.layer_id,
            renderer_type="" if renderer is None else renderer.type(),
        )


# --- setting the renderer: the structured configurations (PRD v2 5.4-7.5) ---

# A colour is six RGB hex digits with no alpha and a length is millimetres:
# those are the only shapes the simple-symbol API here accepts (PRD v2 5.5).
type ColorHex = Annotated[StrictStr, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]
type FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
type PositiveMm = Annotated[float, Field(gt=0, allow_inf_nan=False)]
type Band = Annotated[StrictInt, Field(ge=1)]


class MarkerSymbolConfig(BaseModel):
    """A round marker, the only point symbol this version can configure."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["marker"]
    color: ColorHex
    size_mm: PositiveMm = 2.0


class LineSymbolConfig(BaseModel):
    """A solid line: no dash pattern, no offset."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["line"]
    color: ColorHex
    width_mm: PositiveMm = 0.3


class FillSymbolConfig(BaseModel):
    """A flat fill with a simple outline; the outline cannot be hidden."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["fill"]
    color: ColorHex
    outline_color: ColorHex = "#333333"
    outline_width_mm: PositiveMm = 0.3


type SimpleSymbolConfig = Annotated[
    MarkerSymbolConfig | LineSymbolConfig | FillSymbolConfig,
    Field(discriminator="type"),
]


class NullSymbolRendererConfig(BaseModel):
    """Draws no feature symbol; it is not the same as hiding the layer."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["nullSymbol"]


class SingleSymbolRendererConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["singleSymbol"]
    symbol: SimpleSymbolConfig


class CategoryConfig(BaseModel):
    """One value of the key field and the symbol it is drawn with."""

    model_config = ConfigDict(extra="forbid")

    value: str | int | FiniteFloat | bool | None
    label: StrictStr = Field(min_length=1)
    symbol: SimpleSymbolConfig


class CategorizedRendererConfig(BaseModel):
    """The caller supplies every category: nothing is derived from the data."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["categorizedSymbol"]
    field: StrictStr = Field(min_length=1)
    categories: list[CategoryConfig] = Field(min_length=1)


class RangeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lower: FiniteFloat
    upper: FiniteFloat
    label: StrictStr
    symbol: SimpleSymbolConfig

    @model_validator(mode="after")
    def _lower_is_below_upper(self) -> RangeConfig:
        if self.lower >= self.upper:
            raise ValueError("lower must be below upper")
        return self


class GraduatedRendererConfig(BaseModel):
    """The caller supplies every breakpoint; QGIS classifies nothing itself."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["graduatedSymbol"]
    field: StrictStr = Field(min_length=1)
    ranges: list[RangeConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _ranges_are_ordered(self) -> GraduatedRendererConfig:
        # Adjacent ranges may share an endpoint; gaps are the caller's choice.
        for previous, current in zip(self.ranges, self.ranges[1:], strict=False):
            if current.lower < previous.upper:
                raise ValueError("ranges must be ordered by lower and must not overlap")
        return self


class RuleConfig(BaseModel):
    """A flat rule: a filter expression or the single ELSE, plus its symbol."""

    model_config = ConfigDict(extra="forbid")

    label: StrictStr = Field(min_length=1)
    filter: StrictStr | None = None
    else_rule: StrictBool = False
    symbol: SimpleSymbolConfig


class RuleRendererConfig(BaseModel):
    """Rules keep the caller's order; nesting and scale limits are out of scope."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["RuleRenderer"]
    rules: list[RuleConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_else_and_it_is_last(self) -> RuleRendererConfig:
        for position, rule in enumerate(self.rules):
            if rule.else_rule:
                if rule.filter is not None:
                    raise ValueError("an else rule takes no filter")
                if position != len(self.rules) - 1:
                    raise ValueError("an else rule must be the last rule")
            elif rule.filter is None or not rule.filter.strip():
                raise ValueError("a rule needs a filter expression")
        return self


class SingleBandGrayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["singlebandgray"]
    band: Band
    min: FiniteFloat
    max: FiniteFloat

    @model_validator(mode="after")
    def _min_is_below_max(self) -> SingleBandGrayConfig:
        if self.min >= self.max:
            raise ValueError("min must be below max")
        return self


class MultiBandColorConfig(BaseModel):
    """The same band may name two or three channels."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["multibandcolor"]
    red_band: Band
    green_band: Band
    blue_band: Band


class ColorStop(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: FiniteFloat
    color: ColorHex


class PseudoColorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["singlebandpseudocolor"]
    band: Band
    interpolation: Literal["linear", "discrete", "exact"] = "linear"
    stops: list[ColorStop] = Field(min_length=2)

    @model_validator(mode="after")
    def _stops_increase(self) -> PseudoColorConfig:
        values = [stop.value for stop in self.stops]
        if any(current <= previous for previous, current in zip(values, values[1:], strict=False)):
            raise ValueError("stops must be strictly increasing")
        return self


class SingleColorRasterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["singlecolor"]
    band: Band
    color: ColorHex


type VectorRendererConfig = Annotated[
    NullSymbolRendererConfig
    | SingleSymbolRendererConfig
    | CategorizedRendererConfig
    | GraduatedRendererConfig
    | RuleRendererConfig,
    Field(discriminator="type"),
]

type RasterRendererConfig = Annotated[
    SingleBandGrayConfig | MultiBandColorConfig | PseudoColorConfig | SingleColorRasterConfig,
    Field(discriminator="type"),
]

# One union, so a type outside the nine listed is refused by the model itself.
type RendererConfig = Annotated[
    VectorRendererConfig | RasterRendererConfig,
    Field(discriminator="type"),
]

# The shader ramp type per declared interpolation: values are the names of
# ``QgsColorRampShader.Type`` members (PRD v2 7.3).
PSEUDO_RAMP_TYPES = {"linear": "Interpolated", "discrete": "Discrete", "exact": "Exact"}


class LayerSetRendererParams(Params):
    layer_id: StrictStr = Field(min_length=1)
    renderer: RendererConfig


class LayerSetRendererResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: StrictStr
    renderer_type: StrictStr


def expected_symbol_type(layer: Any) -> Literal["marker", "line", "fill"]:
    """The simple symbol a layer's geometry takes; Multi* reports its base type."""

    from qgis.core import Qgis

    geometry = layer.geometryType()
    if geometry == Qgis.GeometryType.Point:
        return "marker"
    if geometry == Qgis.GeometryType.Line:
        return "line"
    if geometry == Qgis.GeometryType.Polygon:
        return "fill"
    raise FrameworkError(INVALID_LAYER, "the layer has no supported geometry")


def simple_symbol(config: SimpleSymbolConfig, expected: str) -> Any:
    """One native symbol from the declared colour and size alone (PRD v2 5.5)."""

    if config.type != expected:
        raise FrameworkError(INVALID_INPUT, "the symbol does not match the layer geometry")
    if config.type == "marker":
        from qgis.core import QgsMarkerSymbol

        return QgsMarkerSymbol.createSimple({"color": config.color, "size": config.size_mm})
    if config.type == "line":
        from qgis.core import QgsLineSymbol

        return QgsLineSymbol.createSimple({"color": config.color, "width": config.width_mm})
    from qgis.core import QgsFillSymbol

    return QgsFillSymbol.createSimple(
        {
            "color": config.color,
            "outline_color": config.outline_color,
            "outline_width": config.outline_width_mm,
        }
    )


def layer_field(layer: Any, name: str) -> Any:
    """The layer's own field, so a value is checked against its real type."""

    fields = layer.fields()
    index = fields.indexFromName(name)
    if index < 0:
        raise FrameworkError(INVALID_FIELD_TYPE, "the layer has no such field")
    return fields.at(index)


def category_key(field: Any, value: Any) -> tuple[tuple[str, Any], Any]:
    """The key two categories are compared by, and the value the renderer stores.

    An integer and a float are the same numeric category.  Every other value is
    converted to the field's own type first: a value the field cannot hold is
    refused instead of becoming a category that can never match, and two spellings
    of one value (``True`` and ``"true"``) are one category (PRD v2 6.3).  An
    integer field takes only whole values inside its own range, and stores them as
    integers -- the same shape its own values have.  Qt compares category values
    numerically (``2`` and ``2.0`` both match a feature value of 2), so the stored
    shape is about the value the field can hold, not about the match.
    """

    if value is None:
        return ("null", None), None

    from qgis.PyQt.QtCore import QVariant

    if field.isNumeric():
        if isinstance(value, (bool, str)):
            raise FrameworkError(INVALID_INPUT, "a category value does not fit the field type")
        try:
            number = float(value)
        except OverflowError:
            # An integer the float cannot hold is not finite for this purpose, and it
            # must not reach either the dedupe key or the native category.
            raise FrameworkError(
                INVALID_INPUT, "a category value does not fit the field type"
            ) from None
        if field.type() in (QVariant.Int, QVariant.LongLong):
            # A fraction on an integer field can never match a feature; a whole value is
            # stored and deduped as an integer, so 2 and 2.0 are one category while the
            # two int64 values a float cannot tell apart stay two.
            # UInt/ULongLong are left out: no provider in this build makes such a field.
            if not number.is_integer():
                raise FrameworkError(INVALID_INPUT, "a category value does not fit the field type")
            whole = int(value)
            # An integer outside int32/int64 is not a value the field can hold (the memory
            # provider refuses such an attribute, and PyQt overflows for int64), so its
            # category would never match.  QgsField.convertCompatible wraps a LongLong
            # overflow instead of refusing it (2**63 -> -2**63, measured), hence the bounds.
            if field.type() == QVariant.Int:
                low, high = -(2**31), 2**31 - 1
            else:
                low, high = -(2**63), 2**63 - 1
            if not low <= whole <= high:
                raise FrameworkError(INVALID_INPUT, "a category value does not fit the field type")
            return ("number", whole), whole
        return ("number", number), value

    variant = QVariant(value)
    if not variant.convert(field.type()):
        raise FrameworkError(INVALID_INPUT, "a category value does not fit the field type")
    # The converted value -- a QDate for a date field, a bool for a boolean one -- is
    # what the field can hold, so it is both the dedupe key and the native value.
    converted = variant.value()
    return ("value", converted), converted


def raster_band(layer: Any, band: int) -> int:
    """Bands are 1-based and only the layer's own bands are addressable."""

    if not 1 <= band <= layer.bandCount():
        raise FrameworkError(INVALID_INPUT, "a band is outside the raster's bands")
    return band


RASTER_RENDERER_CONFIGS = (
    SingleBandGrayConfig,
    MultiBandColorConfig,
    PseudoColorConfig,
    SingleColorRasterConfig,
)


def build_renderer(layer: Any, config: RendererConfig) -> Any:
    """The complete native renderer; the layer is not touched here (PRD v2 5.4)."""

    if isinstance(config, RASTER_RENDERER_CONFIGS):
        if layer_kind(layer) != "raster":
            raise FrameworkError(INVALID_LAYER, "the renderer does not apply to this layer")
        return raster_renderer(layer, config)
    if layer_kind(layer) != "vector":
        raise FrameworkError(INVALID_LAYER, "the renderer does not apply to this layer")
    return vector_renderer(layer, config)


def vector_renderer(layer: Any, config: VectorRendererConfig) -> Any:
    """Build one of the five vector renderers, symbols included."""

    from qgis.core import (
        QgsCategorizedSymbolRenderer,
        QgsExpression,
        QgsGraduatedSymbolRenderer,
        QgsNullSymbolRenderer,
        QgsRendererCategory,
        QgsRendererRange,
        QgsRuleBasedRenderer,
        QgsSingleSymbolRenderer,
    )

    expected = expected_symbol_type(layer)
    if isinstance(config, NullSymbolRendererConfig):
        return QgsNullSymbolRenderer()
    if isinstance(config, SingleSymbolRendererConfig):
        return QgsSingleSymbolRenderer(simple_symbol(config.symbol, expected))
    if isinstance(config, CategorizedRendererConfig):
        field = layer_field(layer, config.field)
        seen: set[tuple[str, Any]] = set()
        categories = []
        for entry in config.categories:
            key, value = category_key(field, entry.value)
            if key in seen:
                raise FrameworkError(INVALID_INPUT, "a category value repeats")
            seen.add(key)
            categories.append(
                QgsRendererCategory(value, simple_symbol(entry.symbol, expected), entry.label)
            )
        return QgsCategorizedSymbolRenderer(config.field, categories)
    if isinstance(config, GraduatedRendererConfig):
        if not layer_field(layer, config.field).isNumeric():
            raise FrameworkError(INVALID_FIELD_TYPE, "the field is not a number")
        ranges = [
            QgsRendererRange(
                entry.lower, entry.upper, simple_symbol(entry.symbol, expected), entry.label
            )
            for entry in config.ranges
        ]
        return QgsGraduatedSymbolRenderer(config.field, ranges)

    # A rule's filter is only checked for syntax: its result is never evaluated here.
    for rule in config.rules:
        if not rule.else_rule and QgsExpression(rule.filter or "").hasParserError():
            raise FrameworkError(INVALID_INPUT, "a rule filter is not a valid expression")
    root = QgsRuleBasedRenderer.Rule(None)
    for rule in config.rules:
        child = QgsRuleBasedRenderer.Rule(
            simple_symbol(rule.symbol, expected),
            filterExp=rule.filter or "",
            label=rule.label,
            elseRule=rule.else_rule,
        )
        child.setActive(True)
        root.appendChild(child)
    return QgsRuleBasedRenderer(root)


def raster_renderer(layer: Any, config: RasterRendererConfig) -> Any:
    """Build one of the four raster renderers from the layer's own provider."""

    from qgis.core import (
        QgsColorRampShader,
        QgsContrastEnhancement,
        QgsMultiBandColorRenderer,
        QgsRasterShader,
        QgsRasterSingleColorRenderer,
        QgsSingleBandGrayRenderer,
        QgsSingleBandPseudoColorRenderer,
    )
    from qgis.PyQt.QtGui import QColor

    provider = layer.dataProvider()
    if isinstance(config, MultiBandColorConfig):
        red, green, blue = (
            raster_band(layer, band)
            for band in (config.red_band, config.green_band, config.blue_band)
        )
        return QgsMultiBandColorRenderer(provider, red, green, blue)
    band = raster_band(layer, config.band)
    if isinstance(config, SingleBandGrayConfig):
        renderer = QgsSingleBandGrayRenderer(provider, band)
        # The enhancement takes the band's own data type, or a float band's values
        # would be clipped to an 8-bit range before the stretch sees them.
        enhancement = QgsContrastEnhancement(provider.dataType(band))
        # The algorithm setter below builds the lookup table once; the two values
        # only store their bounds (PRD v2 7.1).
        enhancement.setMinimumValue(config.min, False)
        enhancement.setMaximumValue(config.max, False)
        enhancement.setContrastEnhancementAlgorithm(QgsContrastEnhancement.StretchToMinimumMaximum)
        renderer.setContrastEnhancement(enhancement)
        return renderer
    if isinstance(config, PseudoColorConfig):
        ramp = QgsColorRampShader()
        ramp.setColorRampType(getattr(QgsColorRampShader, PSEUDO_RAMP_TYPES[config.interpolation]))
        ramp.setColorRampItemList(
            [
                QgsColorRampShader.ColorRampItem(stop.value, QColor(stop.color))
                for stop in config.stops
            ]
        )
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(ramp)
        return QgsSingleBandPseudoColorRenderer(provider, band, shader)
    return QgsRasterSingleColorRenderer(provider, band, QColor(config.color))


class LayerSetRenderer(
    BaseCommand[LayerSetRendererParams, LayerSetRendererResult],
    MainThreadOperationMixin[LayerSetRendererParams, LayerSetRendererResult],
):
    """Replace one layer's whole renderer in a single call (PRD v2 5.4).

    Every refusal happens before ``setRenderer``: the old renderer is still the
    one installed.  Once installed, the layer owns the object; only its type name
    is reported, so nothing here keeps using it.
    """

    name = "layer.set_renderer"
    description = "Replace one layer's renderer with a structured configuration"
    input_model = LayerSetRendererParams
    output_model = LayerSetRendererResult
    destructive = True

    def execute_main(
        self, ctx: OperationContext, request: LayerSetRendererParams
    ) -> LayerSetRendererResult:
        layer = layer_for_style(ctx.project, request.layer_id)
        try:
            renderer = build_renderer(layer, request.renderer)
            layer.setRenderer(renderer)
            layer.triggerRepaint()
            ctx.project.setDirty(True)
            renderer_type = layer.renderer().type()
        except FrameworkError:
            raise
        except Exception as error:  # noqa: BLE001 - one native failure is one refusal
            # The native message may name a field or a file, so it stays out of the record.
            raise FrameworkError(INTERNAL_ERROR, "the renderer could not be set") from error
        return LayerSetRendererResult(layer_id=request.layer_id, renderer_type=renderer_type)
