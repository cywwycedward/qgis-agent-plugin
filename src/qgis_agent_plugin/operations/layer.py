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
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator

from ..core.contracts import (
    BackgroundOperationMixin,
    BaseCommand,
    BaseQuery,
    MainThreadOperationMixin,
    OperationContext,
    TaskControl,
)
from ..core.models import (
    INVALID_FIELD_TYPE,
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
        layer = ctx.project.mapLayer(request.layer_id)
        if layer is None:
            raise FrameworkError(LAYER_NOT_FOUND, "layer does not exist")
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
        if ctx.project.mapLayer(request.layer_id) is None:
            raise FrameworkError(LAYER_NOT_FOUND, "layer does not exist")
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
    """The sum of a snapshot, which is not the layer's value when the task ends."""

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

        layer = ctx.project.mapLayer(request.layer_id)
        if layer is None:
            raise FrameworkError(LAYER_NOT_FOUND, "layer does not exist")
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
