"""图层操作：载入本地文件、看图层、切可见性、求和，以及读写样式与渲染器。

除 ``layer.sum_numeric_field`` 外都跑在主线程并经 ``ctx.project``，读的是引擎持有的工程，不是全局
单例。图层从不离开建它的方法：出去的只有 id、名字、provider、CRS、可见性标志和字符串。

Every operation but ``layer.sum_numeric_field`` runs on the main thread through ``ctx.project``, so
it reads the project the engine holds rather than a global singleton.  A layer never leaves the
method that built it: only ids, names, providers, CRSs, visibility flags and strings travel out.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr  # pyright: ignore[reportMissingImports]

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
    STALE_PROJECT,
    FrameworkError,
    Params,
    Result,
)
from ..core.security import absolute_path

# 调用方可以请求载入什么；``other`` 只在读的那一侧出现。
# What a caller may ask to load; ``other`` exists only on the reading side.
LayerKind = Literal["vector", "raster"]
LayerSummaryKind = Literal["vector", "raster", "other"]


class LayerAddParams(Params):
    """一个要载入的本地文件，名字由调用方定。

    One local file to load, under a name the caller chooses.
    """

    kind: LayerKind
    source_path: StrictStr = Field(min_length=1)
    name: StrictStr = Field(min_length=1)
    # 名字是 provider 的自由文本（空格、斜杠、非 ASCII 都行），但空不是名字；矢量与栅格都有这一项，
    # 因为两个页签都提供子数据集清单。
    # The name is the provider's free text (spaces, slashes, non-ASCII); an empty string is not a
    # name.  Both kinds have one, because both tabs offer a subdataset list.
    sub_layer: StrictStr | None = Field(default=None, min_length=1)


class LayerAddResult(Result):
    layer_id: StrictStr
    name: StrictStr
    kind: LayerKind
    provider: StrictStr
    crs_authid: StrictStr


class LayerSummary(BaseModel):
    """一个已注册图层，说成调用方能据以行动的样子。

    One registered layer in the terms a caller can act on.
    """

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


class LayerListResult(Result):
    layers: list[LayerSummary]


class LayerGetInfoParams(Params):
    layer_id: StrictStr = Field(min_length=1)


class LayerGetInfoResult(Result):
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


class LayerSetVisibilityResult(Result):
    layer_id: StrictStr
    visible: StrictBool
    effective_visible: StrictBool


def project_layer(project: Any, layer_id: str) -> Any:
    """id 背后的图层，没有就是 ``LAYER_NOT_FOUND``；每个读取都从这里开始。

    The layer behind an id, or ``LAYER_NOT_FOUND``; every reader starts here.
    """

    layer = project.mapLayer(layer_id)
    if layer is None:
        raise FrameworkError(LAYER_NOT_FOUND, "layer does not exist")
    return layer


def layer_kind(layer: Any) -> LayerSummaryKind:
    """给一个已注册图层分类；QGIS 装得下的东西都列出来，不拒。

    Classify a registered layer; anything QGIS can hold is listed, not refused.
    """

    from qgis.core import QgsRasterLayer, QgsVectorLayer  # pyright: ignore[reportMissingImports]

    if isinstance(layer, QgsVectorLayer):
        return "vector"
    if isinstance(layer, QgsRasterLayer):
        return "raster"
    return "other"


def layer_tree_node(project: Any, layer_id: str) -> Any | None:
    """图层在当前工程树里的节点；没有节点就是 ``None``。

    The layer's node in the current project's tree, or ``None`` if it has none.
    """

    return project.layerTreeRoot().findLayer(layer_id)


def layer_visibility(project: Any, layer_id: str) -> tuple[bool, bool | None, bool | None]:
    """一个图层的 ``(in_layer_tree, visible, effective_visible)``。

    没有节点的图层没有自己的可见性；非空间图层（比如属性表）在图例里根本没有勾选框。这两种情况下
    两个标志都是 ``None``，而不是假称它被隐藏了。

    A layer without a node has no visibility of its own, and a non-spatial layer has no checkbox in
    the legend at all.  Both flags are ``None`` in either case, rather than a false claim that the
    layer is hidden.
    """

    node = layer_tree_node(project, layer_id)
    if node is None:
        return False, None, None
    layer = node.layer()
    if layer is not None and not layer.isSpatial():
        return True, None, None
    return True, bool(node.itemVisibilityChecked()), bool(node.isVisible())


def layer_summary(project: Any, layer_id: str, layer: Any) -> LayerSummary:
    """摘要唯一生成的地方，所以 list 与 get_info 不可能说法不一致。

    The one place a summary is built, so list and get_info cannot disagree.
    """

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


def queried_sublayers(metadata: Any, source: Path, kind: LayerKind) -> list[Any] | None:
    """provider 为这个源列出的、与声明类型相同的子图层；provider 不会列时是 ``None``。

    清单为空是"provider 不认得这个源"，不是"图层读不出来"：认出来的子图层照样进图例，读不出来的
    靠坏图层图标提示。

    The sublayers the provider lists for this source, filtered to the declared kind, or ``None``
    when the provider cannot list any.  An empty list means "the provider does not recognise this
    source", not "the layer does not load": a recognised sublayer still enters the legend, and a
    broken one is left to the bad-layer icon.
    """

    from qgis.core import Qgis, QgsProviderMetadata  # pyright: ignore[reportMissingImports]

    if not metadata.capabilities() & QgsProviderMetadata.QuerySublayers:
        return None
    wanted = Qgis.LayerType.Vector if kind == "vector" else Qgis.LayerType.Raster
    return [
        detail
        for detail in metadata.querySublayers(
            str(source), Qgis.SublayerQueryFlag.IncludeSystemTables
        )
        if detail.type() == wanted
    ]


def layer_source_uri(provider: str, source: Path, kind: LayerKind, sub_layer: str | None) -> str:
    """要交给图层构造器的 URI：调用方点名的那个子图层，或清单里唯一的那一个。

    子图层的 URI 直接取那条 detail 的 ``uri()``，不自己拼：GDAL 把子数据集写成别的形状，拼出来的
    字符串解不回来。源里有好几个子图层而调用方没点名时不猜一个，而是把名字列出来交回去。

    The URI to hand the layer constructor: the sublayer the caller named, or the only one in the
    list.  A sublayer's URI is that detail's own ``uri()``, never built by hand: GDAL spells
    subdatasets differently and a derived string does not decode back.  When the source holds
    several sublayers and the caller named none, nothing is guessed -- the names go back instead.

    shortcut: this vets the file the caller named, not the sources that file itself names -- a
    local ``.vrt`` may carry a ``/vsicurl/`` or WMS source inside it; upgrade to reading a
    container's own sources if untrusted VRTs become an input.
    """

    from qgis.core import QgsProviderRegistry, QgsProviderUtils  # pyright: ignore[reportMissingImports]

    metadata = QgsProviderRegistry.instance().providerMetadata(provider)
    details = queried_sublayers(metadata, source, kind) if metadata is not None else None
    if details is None:
        # 不能列子图层的 provider 也给不出子图层的 URI，它那个分支不提供子数据集选择。
        # A provider that cannot list sublayers cannot name one either; that branch offers no
        # subdataset choice at all.
        if sub_layer is not None:
            raise FrameworkError(INVALID_LAYER, f"{source} does not offer sub-layers")
        return str(source)
    if not details:
        raise FrameworkError(INVALID_LAYER, f"{source} is not a valid or recognized data source.")
    if sub_layer is not None:
        for detail in details:
            if detail.name() == sub_layer:
                return detail.uri()
        raise FrameworkError(INVALID_LAYER, f'{source} has no sub-layer named "{sub_layer}"')
    incomplete = QgsProviderUtils.sublayerDetailsAreIncomplete(
        details, QgsProviderUtils.SublayerCompletenessFlag.IgnoreUnknownFeatureCount
    )
    if len(details) == 1 and not incomplete:
        return details[0].uri()
    names = ", ".join(sorted(detail.name() for detail in details))
    raise FrameworkError(INVALID_LAYER, f"{source} does not name one of its sub-layers: {names}")


class LayerAdd(
    BaseCommand[LayerAddParams, LayerAddResult],
    MainThreadOperationMixin[LayerAddParams, LayerAddResult],
):
    """把一个矢量或栅格数据源载入当前工程。

    Load a vector or raster data source into the current project.
    """

    name = "layer.add"
    description = "Add a vector or raster data source to the project"
    input_model = LayerAddParams
    output_model = LayerAddResult

    def execute_main(self, ctx: OperationContext, request: LayerAddParams) -> LayerAddResult:
        from qgis.core import QgsRasterLayer, QgsVectorLayer  # pyright: ignore[reportMissingImports]

        source = absolute_path(request.source_path)
        # provider 是声明类型算出来的，所以调用方不能自己指定一个。
        # The provider is a function of the declared kind, so a caller cannot name one.
        provider = "ogr" if request.kind == "vector" else "gdal"
        # 栅格页签在问 provider 之前先问这个文件像不像栅格，不像就一个都不加。
        # The raster tab asks whether the file even looks like a raster before it asks the provider,
        # and adds nothing when it does not.
        if request.kind == "raster" and not QgsRasterLayer.isValidRasterFileName(str(source)):
            raise FrameworkError(INVALID_LAYER, f"{source} is not a supported raster data source")
        layer_class = QgsVectorLayer if request.kind == "vector" else QgsRasterLayer
        uri = layer_source_uri(provider, source, request.kind, request.sub_layer)
        layer = layer_class(uri, request.name, provider)
        # 不查 isValid()：认出来的子图层直接进图例，读不出来的靠坏图层图标提示。
        # 永远进图例：另一个入口里的"先进工程、随后进分组"也仍然让图层留在图例里。
        # No isValid() check: a recognised sublayer enters the legend as it is, and a broken one is
        # left to the bad-layer icon.  Always into the legend: the other entry point's "into the
        # project, then into a group" still leaves the layer in the legend.
        ctx.project.addMapLayer(layer)
        return LayerAddResult(
            layer_id=layer.id(),
            name=layer.name(),
            kind=request.kind,
            provider=provider,
            crs_authid=layer.crs().authid(),
        )


def project_layer_ids_in_legend_order(project: Any) -> list[str]:
    """图例里从上到下的图层 id，后面接上没有树节点的那些。

    图例显示的是图层树的顺序，与 ``mapLayers()`` 的 id 顺序不是一回事。工程里可以有一个没有节点的
    图层（加载来的工程能这样），它不在图例里，所以排在后面，而不是从答案里消失。

    The layer ids the legend shows from top to bottom, then the ones without a node.  The legend
    follows the layer tree, which is not the id order ``mapLayers()`` hands out.  A loaded project
    can hold a layer with no node at all; it is not in the legend, so it comes last rather than
    vanishing from the answer.
    """

    from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer  # pyright: ignore[reportMissingImports]

    ordered: list[str] = []

    def walk(node: Any) -> None:
        for child in node.children():
            if isinstance(child, QgsLayerTreeLayer):
                ordered.append(child.layerId())
            elif isinstance(child, QgsLayerTreeGroup):
                walk(child)

    walk(project.layerTreeRoot())
    listed = set(ordered)
    ordered.extend(layer_id for layer_id in project.mapLayers() if layer_id not in listed)
    return ordered


class LayerList(
    BaseQuery[LayerListParams, LayerListResult],
    MainThreadOperationMixin[LayerListParams, LayerListResult],
):
    """每个已注册图层，按图例从上到下的顺序。

    Every registered layer, in the order the legend shows them.
    """

    name = "layer.list"
    description = "List the layers registered in the project"
    input_model = LayerListParams
    output_model = LayerListResult

    def execute_main(self, ctx: OperationContext, request: LayerListParams) -> LayerListResult:
        summaries = []
        for layer_id in project_layer_ids_in_legend_order(ctx.project):
            layer = ctx.project.mapLayer(layer_id)
            if layer is None:
                continue
            summaries.append(layer_summary(ctx.project, layer_id, layer))
        return LayerListResult(layers=summaries)


class LayerGetInfo(
    BaseQuery[LayerGetInfoParams, LayerGetInfoResult],
    MainThreadOperationMixin[LayerGetInfoParams, LayerGetInfoResult],
):
    """一个图层的身份与状态，包括它的源现在还载不载得进。

    One layer's identity and state, including whether its source still loads.
    """

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
    """切一个图层在图例里的勾选；勾上时连祖先分组一起勾，取消只动这一个节点。

    勾选框不是"只改自己"：对一个叶子节点打勾会连它的祖先一起勾，否则勾了也看不见；取消只写节点
    自己。写入口自己对同值是空操作，所以已经对了就不需要先判断。

    Toggle one layer's legend check; checking also checks the ancestor groups, unchecking touches
    only this node.  Ticking a leaf's ancestors along with it is what makes the tick show anything;
    unchecking writes the node alone.  The setters are no-ops on an unchanged value, so there is
    nothing to decide before writing.
    """

    name = "layer.set_visibility"
    description = "Check or uncheck one layer in the layer tree"
    input_model = LayerSetVisibilityParams
    output_model = LayerSetVisibilityResult

    def execute_main(
        self, ctx: OperationContext, request: LayerSetVisibilityParams
    ) -> LayerSetVisibilityResult:
        layer = project_layer(ctx.project, request.layer_id)
        node = layer_tree_node(ctx.project, request.layer_id)
        if node is None:
            raise FrameworkError(LAYER_NOT_IN_TREE, "layer has no layer tree node")
        if not layer.isSpatial():
            raise FrameworkError(INVALID_LAYER, "the layer is not spatial, so it has no visibility")
        before = (bool(node.itemVisibilityChecked()), bool(node.isVisible()))
        if request.visible:
            node.setItemVisibilityCheckedParentRecursive(True)
        else:
            node.setItemVisibilityChecked(False)
        # 图例的勾选会置脏工程，所以只有真的改了东西才置脏；同值重写不算改动。
        # A legend tick marks the project dirty, so only a write that changed something does; a
        # rewrite of the same value is not a change.
        if (bool(node.itemVisibilityChecked()), bool(node.isVisible())) != before:
            ctx.project.setDirty(True)
        return LayerSetVisibilityResult(
            layer_id=request.layer_id,
            visible=bool(node.itemVisibilityChecked()),
            effective_visible=bool(node.isVisible()),
        )


class LayerSumNumericFieldParams(Params):
    layer_id: StrictStr = Field(min_length=1)
    field_name: StrictStr = Field(min_length=1)
    # 面板上的"仅选中要素"开关，默认不勾：有选择集时照样算整层。
    # The panel's "selected features only" switch, unchecked by default: a selection alone still
    # means the whole layer.
    selected_only: StrictBool = False


class LayerSumNumericFieldResult(Result):
    """一份快照的和，不是任务结束时图层上的值。

    shortcut: 值按 float64 求和，所以值超过 2**53 的 QInt64 字段在 ``total`` 里会丢精度；
    需要精确整数和时再补一条整数路径。

    The sum of a snapshot, which is not the layer's value when the task ends.

    shortcut: the values are summed as float64, so a QInt64 field with values above 2**53 loses
    precision in ``total``; add an integer path when an exact integer sum is needed.
    """

    layer_id: StrictStr
    field_name: StrictStr
    count: int
    skipped_null: int
    total: float
    selected_only: StrictBool
    snapshot_project_epoch: int


@dataclass(frozen=True)
class FieldValues:
    """拷出来的一份字段：纯 float，所以没有 QGIS 对象进后台线程。

    A copied-out field: plain floats, so no QGIS object reaches the worker.
    """

    layer_id: str
    field_name: str
    values: tuple[float, ...]
    skipped_null: int
    selected_only: bool
    project_epoch: int


@dataclass(frozen=True)
class FieldSum:
    """后台算出来的东西，连同它据以计算的工程代数。

    What the worker computed, with the generation it was computed from.
    """

    count: int
    total: float
    skipped_null: int
    selected_only: bool
    project_epoch: int


class LayerSumNumericField(
    BaseQuery[LayerSumNumericFieldParams, LayerSumNumericFieldResult],
    BackgroundOperationMixin[
        LayerSumNumericFieldParams, LayerSumNumericFieldResult, FieldValues, FieldSum
    ],
):
    """在主线程之外、在一份快照上求一个数值字段的和。

    任务声明了 ``dependent_layer_ids``，于是适配层能把它交给 ``QgsTask.setDependentLayers``：
    移除图层就会取消任务，就像 QGIS 对任何依赖任务做的那样。

    The task declares ``dependent_layer_ids`` so the adapter can hand it to
    ``QgsTask.setDependentLayers``: removing the layer then cancels the task, as QGIS does for any
    dependent task.
    """

    name = "layer.sum_numeric_field"
    description = "Sum one numeric field of a vector layer over a copied snapshot"
    input_model = LayerSumNumericFieldParams
    output_model = LayerSumNumericFieldResult

    def prepare_main(
        self, ctx: OperationContext, request: LayerSumNumericFieldParams
    ) -> FieldValues:
        from qgis.core import NULL, QgsFeatureRequest, QgsVectorLayer  # pyright: ignore[reportMissingImports]

        layer = project_layer(ctx.project, request.layer_id)
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            raise FrameworkError(INVALID_LAYER, "the layer is not a valid vector layer")
        fields = layer.fields()
        index = fields.indexFromName(request.field_name)
        if index < 0:
            raise FrameworkError(
                INVALID_FIELD_TYPE,
                f"Invalid field for statistics: “{request.field_name}” does not exist",
            )
        if not fields.at(index).isNumeric():
            raise FrameworkError(INVALID_FIELD_TYPE, "the field is not a number")

        # 求和只读一个字段、不要几何：请求裁剪到那一列，主线程不为别的列和每个几何付拷贝费。
        # The sum reads one column and no geometry: the request is trimmed to that column, so the
        # main thread does not pay for the other columns or for any geometry.
        read = (
            QgsFeatureRequest()
            .setFlags(QgsFeatureRequest.NoGeometry)
            .setSubsetOfAttributes([index])
        )
        features = (
            layer.getSelectedFeatures(read) if request.selected_only else layer.getFeatures(read)
        )
        values: list[float] = []
        skipped = 0
        for feature in features:
            value = feature[index]
            # 空属性要么是 QGIS 自己的 NULL 单例，要么是 None；两者都算跳过的空值。
            # A null attribute arrives either as QGIS's own NULL singleton or as None; both count as
            # a skipped null.
            if value is None or value == NULL:
                skipped += 1
                continue
            number = float(value)
            if not math.isfinite(number):
                # 一个 NaN 或 Infinite 的值会把和也变成非有限数，而记录必须是 RFC 8259 解析器
                # 读得懂的 JSON；所以在拷贝这一侧就拒，而不是交出一个没人能解析的结果。
                # A NaN or infinite value would make the sum non-finite too, and a record has to be
                # JSON an RFC 8259 parser reads; so it is refused here, while copying, rather than
                # delivering a result nobody can parse.
                raise FrameworkError(
                    INVALID_FIELD_TYPE, "the field holds a non-finite (NaN or infinite) value"
                )
            values.append(number)
        return FieldValues(
            layer_id=request.layer_id,
            field_name=request.field_name,
            values=tuple(values),
            skipped_null=skipped,
            selected_only=request.selected_only,
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
                    # 在这里停是取消的协作那一半；适配层把部分运行变成 CANCELLED 并丢掉数字。
                    # Stopping here is the cooperative half of the cancel; the adapter turns the
                    # partial run into CANCELLED and drops the numbers.
                    break
                control.set_progress(100.0 * position / len(values))
            total += value
            count += 1
        if not math.isfinite(total):
            # 每个值都有限也可能加出 Infinity，所以和本身也要查；非有限数同样进不了记录。
            # Finite values can still add up to Infinity, so the total is checked as well; a
            # non-finite number cannot reach the record either.
            raise FrameworkError(INVALID_FIELD_TYPE, "the sum of the field is not a finite number")
        return FieldSum(
            count=count,
            total=total,
            skipped_null=snapshot.skipped_null,
            selected_only=snapshot.selected_only,
            project_epoch=snapshot.project_epoch,
        )

    def finalize_main(
        self, ctx: OperationContext, request: LayerSumNumericFieldParams, result: FieldSum
    ) -> LayerSumNumericFieldResult:
        if ctx.lifecycle.current_epoch != result.project_epoch:
            raise FrameworkError(
                STALE_PROJECT, "the project was replaced while the field was summed"
            )
        return LayerSumNumericFieldResult(
            layer_id=request.layer_id,
            field_name=request.field_name,
            count=result.count,
            skipped_null=result.skipped_null,
            total=result.total,
            selected_only=result.selected_only,
            snapshot_project_epoch=result.project_epoch,
        )

    def dependent_layer_ids(self, request: LayerSumNumericFieldParams) -> list[str]:
        return [request.layer_id]


# --- 读样式：分类与 QML / reading the style: categories and QML ---

# ``QgsMapLayer.StyleCategory`` 的十八个名字，按 QGIS 声明它们的顺序；栅格只导出其中九项，
# 顺序就是那个图层类型给勾选框的顺序。这里存的是名字不是位标记：位标记只在一次请求里才建。
# The eighteen ``QgsMapLayer.StyleCategory`` names in the order QGIS declares them; a raster
# exports only nine of them, in the order its layer type hands out checkboxes.  What is stored here
# is names, not flags: a flag is only built for one request.
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
    "MapTips",
    "Rendering",
    "CustomProperties",
    "Temporal",
    "Elevation",
    "AttributeTable",
    "Notes",
)


def layer_for_style(project: Any, layer_id: str) -> Any:
    """id 背后的图层，前提它是一个矢量或栅格。

    样式的读写只对这两种形体有定义，所以别人的图层类型是 ``INVALID_LAYER`` 而不是空答案：
    "没有分类"和"不适用"是两个结果。几何不是条件：没有几何的矢量表也有字段、表单、属性表设置这些
    分类，也能导出 QML。

    The layer behind an id, provided it is a vector or a raster.  Style reads and writes are defined
    only for those two shapes, so somebody else's layer type is ``INVALID_LAYER`` rather than an
    empty answer: "no categories" and "not applicable" are different results.  Geometry is not a
    condition: a geometry-less vector table still has fields, forms and attribute-table settings to
    export.

    shortcut: ``QgsMapLayer.StyleCategory`` is a flag enum, which Python cannot enumerate in order;
    upgrade to reading them from the enum if a binding makes it iterable.
    """

    layer = project_layer(project, layer_id)
    if layer_kind(layer) == "other":
        raise FrameworkError(INVALID_LAYER, "the layer is not a vector or raster layer")
    return layer


def layer_for_renderer(project: Any, layer_id: str) -> Any:
    """一个能读、能换渲染器的图层：符号页会给它一个页面的那个。

    矢量属性对话框只看一件事：图层有没有渲染器；没有的话整个符号页被禁用，里面那个下拉框、当前
    渲染器和"换上这个渲染器"的按钮就都不存在。没有几何的矢量表就是这样。栅格属性对话框对无效图层
    直接返回、对没有渲染器的图层不建渲染器页，所以同样装不上。两条路都落到这一句上。

    A layer whose renderer can be read or replaced: the one the Symbology page is offered for.  The
    vector properties dialog looks at one thing -- whether the layer has a renderer.  Without one
    the whole page is disabled, and its combo box, current renderer and install button do not
    exist; a geometry-less vector table is exactly that case.  The raster properties dialog returns
    early for an invalid layer and builds no renderer page when there is no renderer.  Both roads
    end here.
    """

    layer = layer_for_style(project, layer_id)
    if layer.renderer() is None:
        raise FrameworkError(INVALID_LAYER, "the layer has no renderer")
    return layer


def style_categories(layer: Any) -> tuple[StyleCategoryName, ...]:
    """一个已经审过的图层适用的分类，按固定顺序。

    The categories that apply to an already-vetted layer, in the fixed order.
    """

    return RASTER_STYLE_CATEGORIES if layer_kind(layer) == "raster" else VECTOR_STYLE_CATEGORIES


def style_category_flag(names: Sequence[str]) -> Any:
    """恰好点出 ``names`` 的原生位标记；空序列就是原生那个"一个都不选"。

    The native bit flag naming exactly ``names``; an empty sequence is the native "nothing
    selected" flag.
    """

    from qgis.core import QgsMapLayer  # pyright: ignore[reportMissingImports]

    flag = QgsMapLayer.StyleCategories()
    for name in names:
        flag = flag | getattr(QgsMapLayer, name)
    return flag


class LayerListStyleCategoriesParams(Params):
    layer_id: StrictStr = Field(min_length=1)


class LayerListStyleCategoriesResult(Result):
    layer_id: StrictStr
    categories: list[StyleCategoryName]


class LayerGetQmlParams(Params):
    layer_id: StrictStr = Field(min_length=1)
    categories: list[StyleCategoryName] | None = None


class LayerGetQmlResult(Result):
    layer_id: StrictStr
    qml: StrictStr


class LayerListAvailableRenderersParams(Params):
    layer_id: StrictStr = Field(min_length=1)


class LayerListAvailableRenderersResult(Result):
    renderer_types: list[StrictStr]


class LayerGetRendererParams(Params):
    layer_id: StrictStr = Field(min_length=1)


class LayerGetRendererResult(Result):
    layer_id: StrictStr
    renderer_type: StrictStr
    renderer_xml: StrictStr


class LayerSetRendererParams(Params):
    layer_id: StrictStr = Field(min_length=1)
    # 渲染器自己那份文档，与 ``layer.get_renderer`` 交回来的形状相同。
    # The renderer's own document, the same shape ``layer.get_renderer`` hands back.
    renderer: StrictStr = Field(min_length=1)


class LayerSetRendererResult(Result):
    layer_id: StrictStr
    renderer_type: StrictStr


class LayerListStyleCategories(
    BaseQuery[LayerListStyleCategoriesParams, LayerListStyleCategoriesResult],
    MainThreadOperationMixin[LayerListStyleCategoriesParams, LayerListStyleCategoriesResult],
):
    """这种图层类型有的 QML 分类，不管它们里面有没有东西。

    The QML categories this layer type has, whether or not they are populated.
    """

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
    """一个图层的样式，就是图层自己导出的 QML，只存在内存里。

    One layer's style as the QML the layer itself exports, in memory only.
    """

    name = "layer.get_qml"
    description = "Export one layer's current style as QML XML in memory"
    input_model = LayerGetQmlParams
    output_model = LayerGetQmlResult

    def execute_main(self, ctx: OperationContext, request: LayerGetQmlParams) -> LayerGetQmlResult:
        from qgis.core import QgsMapLayer, QgsReadWriteContext  # pyright: ignore[reportMissingImports]
        from qgis.PyQt.QtXml import QDomDocument  # pyright: ignore[reportMissingImports]

        layer = layer_for_style(ctx.project, request.layer_id)
        # 没点名分类就是全部：那是保存样式对话框里"全选"之后的位标记，而导出只写这门图层类型有的
        # 那几类。
        # Naming no categories means all of them: that is the flag after "select all" in the save
        # dialog, and the export only writes the ones this layer type has.
        categories = (
            QgsMapLayer.AllStyleCategories
            if request.categories is None
            else style_category_flag(request.categories)
        )
        document = QDomDocument()
        message = layer.exportNamedStyle(document, QgsReadWriteContext(), categories)
        if message:
            raise FrameworkError(INTERNAL_ERROR, message)
        return LayerGetQmlResult(layer_id=request.layer_id, qml=document.toString(2))


def available_renderer_types(layer: Any) -> list[str]:
    """这门图层类型的渲染器下拉框里的那些名字，按下拉框的顺序。

    矢量取活的注册表按几何过滤后的清单；栅格取注册表清单再按图层的 ``rasterType()`` 过滤一层，所以
    普通栅格里没有 ``singlebandcolordata``，而 ``SingleBandColorData`` 栅格里只有它。

    The names this layer type's renderer combo holds, in combo order.  A vector takes the live
    registry list filtered by its geometry; a raster takes the registry list filtered once more by
    the layer's own ``rasterType()``, so an ordinary raster has no ``singlebandcolordata`` and a
    ``SingleBandColorData`` raster has only that one.

    shortcut: 3.44 does not expose the raster renderer registry through ``QgsApplication``, so a
    registry object is built here; upgrade to the application-wide one when a binding exposes it.
    """

    from qgis.core import (  # pyright: ignore[reportMissingImports]
        Qgis,
        QgsApplication,
        QgsRasterRendererRegistry,
    )

    if layer_kind(layer) != "raster":
        return QgsApplication.rendererRegistry().renderersList(layer)
    single_band_color_data = layer.rasterType() == Qgis.RasterLayerType.SingleBandColorData
    return [
        name
        for name in QgsRasterRendererRegistry().renderersList()
        if (name == "singlebandcolordata") == single_band_color_data
    ]


class LayerListAvailableRenderers(
    BaseQuery[LayerListAvailableRenderersParams, LayerListAvailableRenderersResult],
    MainThreadOperationMixin[LayerListAvailableRenderersParams, LayerListAvailableRenderersResult],
):
    """活的 QGIS 注册表为这个图层提供的渲染器类型名，按下拉框的顺序。

    The renderer type names the live QGIS registries offer for this layer, in combo order.
    """

    name = "layer.list_available_renderers"
    description = "List the renderer type names the QGIS registries offer for one layer"
    input_model = LayerListAvailableRenderersParams
    output_model = LayerListAvailableRenderersResult

    def execute_main(
        self, ctx: OperationContext, request: LayerListAvailableRenderersParams
    ) -> LayerListAvailableRenderersResult:
        layer = layer_for_renderer(ctx.project, request.layer_id)
        return LayerListAvailableRenderersResult(renderer_types=available_renderer_types(layer))


def renderer_document(layer: Any) -> str:
    """图层当前那个渲染器自己的 XML 文档。

    矢量渲染器把自己写成一个元素交回来；栅格渲染器写进容器的 ``rasterrenderer`` 子元素，所以先建
    容器再把它取出来当根。两种都成为独立文档，读回来时喂给同一个重建入口。

    The layer's current renderer as its own XML document.  A vector renderer hands back an element
    of its own; a raster renderer writes into a ``rasterrenderer`` child of a container, so the
    container is built and that child becomes the root.  Both become a standalone document that the
    installer reads back.

    shortcut: the XML carries everything the renderer needs except style-database names, which a
    symbol only uses as a label; upgrade if a renderer ever depends on a style it does not embed.
    """

    from qgis.core import QgsReadWriteContext  # pyright: ignore[reportMissingImports]
    from qgis.PyQt.QtXml import QDomDocument  # pyright: ignore[reportMissingImports]

    document = QDomDocument()
    renderer = layer.renderer()
    if layer_kind(layer) == "raster":
        # 栅格渲染器把自己写成容器的子元素，所以先建容器，再把它从容器里摘出来当根：这是同一份
        # 文档内的移动，不需要跨文档导入。
        # A raster renderer writes itself as a child of a container, so the container is built and
        # that child is then detached and made the root: a move inside one document, which needs no
        # cross-document import.
        container = document.createElement("qgis")
        document.appendChild(container)
        renderer.writeXml(document, container)
        element = container.firstChildElement("rasterrenderer")
        container.removeChild(element)
        document.removeChild(container)
        document.appendChild(element)
    else:
        element = renderer.save(document, QgsReadWriteContext())
        if element.parentNode().isNull():
            document.appendChild(element)
    return document.toString(2)


def raster_renderer(provider: Any, element: Any) -> Any | None:
    """按文档里的类型重建栅格渲染器；不认识的类型是 ``None``。

    每个类都有自己的 ``create(element, input)``，它就是注册表里那个建渲染器的函数：波段、对比增强、
    色带都由它从文档里读回来。Python 拿不到注册表里的函数指针，所以"类型名到函数"这一步只能在这里
    写明。

    Rebuild the raster renderer the document names; an unknown type is ``None``.  Each class has its
    own ``create(element, input)`` -- the renderer-creating function the registry holds -- and it is
    what reads the bands, contrast enhancement and colour ramp back out of the document.  Python
    cannot reach the registry's function pointer, so the mapping from type name to function is
    spelled out here.
    """

    from qgis.core import (  # pyright: ignore[reportMissingImports]
        QgsHillshadeRenderer,
        QgsMultiBandColorRenderer,
        QgsPalettedRasterRenderer,
        QgsRasterContourRenderer,
        QgsRasterSingleColorRenderer,
        QgsSingleBandColorDataRenderer,
        QgsSingleBandGrayRenderer,
        QgsSingleBandPseudoColorRenderer,
    )

    builders = {
        "multibandcolor": QgsMultiBandColorRenderer.create,
        "paletted": QgsPalettedRasterRenderer.create,
        "singlebandgray": QgsSingleBandGrayRenderer.create,
        "singlebandpseudocolor": QgsSingleBandPseudoColorRenderer.create,
        "singlebandcolordata": QgsSingleBandColorDataRenderer.create,
        "singlecolor": QgsRasterSingleColorRenderer.create,
        "hillshade": QgsHillshadeRenderer.create,
        "contour": QgsRasterContourRenderer.create,
    }
    build = builders.get(element.attribute("type"))
    if build is None:
        return None
    return build(element, provider)


def renderer_from_document(layer: Any, text: str) -> Any:
    """把一份渲染器文档变成这门图层能装的原生渲染器；读不出来就是 ``INVALID_INPUT``。

    顺序是先解析、再读类型、再建：类型名在文档的根元素上，解析完就有；建那一步会进 QGIS 的注册表
    去调对应元数据的 ``createRenderer``，所以不在候选清单里的类型必须在建之前拦下（清单里的类型由
    注册表建，栅格的八个由各自的 ``create`` 建）。

    Parse, then read the type, then build: the type name sits on the document's root element and is
    there as soon as it parses, while building enters QGIS's registry and calls that metadata's
    ``createRenderer``.  So a type outside the candidate list has to be refused before it is built;
    the ones inside it are built by the registry (the eight raster types by their own ``create``).
    """

    from qgis.core import QgsFeatureRenderer, QgsReadWriteContext  # pyright: ignore[reportMissingImports]
    from qgis.PyQt.QtXml import QDomDocument  # pyright: ignore[reportMissingImports]

    document = QDomDocument()
    parsed, message, line, column = document.setContent(text)
    if not parsed:
        raise FrameworkError(INVALID_INPUT, f"{message} at line {line} column {column}")
    element = document.documentElement()
    renderer_type = element.attribute("type")
    if not renderer_type:
        raise FrameworkError(INVALID_INPUT, "the document is not a renderer this layer can use")
    # 拒绝的理由分两半：栅格那边实测有类型会被 QGIS 静默保留（普通栅格上的 singlebandcolordata），
    # 矢量那边是下拉框按几何筛过、GUI 根本表达不出这个组合。
    # The refusal has two reasons: on the raster side QGIS was measured to keep the old renderer
    # silently for one type (singlebandcolordata on an ordinary raster), while the vector combo is
    # filtered by geometry, so the GUI cannot express that combination at all.
    if renderer_type not in available_renderer_types(layer):
        raise FrameworkError(INVALID_INPUT, f"the layer does not offer a {renderer_type} renderer")
    if layer_kind(layer) == "raster":
        renderer = raster_renderer(layer.dataProvider(), element)
    else:
        renderer = QgsFeatureRenderer.load(element, QgsReadWriteContext())
    if renderer is None:
        raise FrameworkError(INVALID_INPUT, "the document is not a renderer this layer can use")
    return renderer


class LayerGetRenderer(
    BaseQuery[LayerGetRendererParams, LayerGetRendererResult],
    MainThreadOperationMixin[LayerGetRendererParams, LayerGetRendererResult],
):
    """图层当前那个渲染器的类型名与它自己的文档。

    The type name of the renderer the layer currently has, and the renderer's own document.
    """

    name = "layer.get_renderer"
    description = "Read one layer's current renderer type and document"
    input_model = LayerGetRendererParams
    output_model = LayerGetRendererResult

    def execute_main(
        self, ctx: OperationContext, request: LayerGetRendererParams
    ) -> LayerGetRendererResult:
        layer = layer_for_renderer(ctx.project, request.layer_id)
        return LayerGetRendererResult(
            layer_id=request.layer_id,
            renderer_type=layer.renderer().type(),
            renderer_xml=renderer_document(layer),
        )


class LayerSetRenderer(
    BaseCommand[LayerSetRendererParams, LayerSetRendererResult],
    MainThreadOperationMixin[LayerSetRendererParams, LayerSetRendererResult],
):
    """用一次调用换掉一个图层的整个渲染器。

    渲染器先整份建好再装，所以读不出来的文档不会给图层留下半套样式，旧渲染器也还在。装上去之后图层
    拥有那个对象，这里只报它的类型名。

    Replace one layer's whole renderer in a single call.  The renderer is built whole before the
    layer is touched, so a document that does not read leaves the old renderer in place.  Once
    installed, the layer owns the object; only its type name is reported.
    """

    name = "layer.set_renderer"
    description = "Replace one layer's renderer with a renderer document"
    input_model = LayerSetRendererParams
    output_model = LayerSetRendererResult
    destructive = True

    def execute_main(
        self, ctx: OperationContext, request: LayerSetRendererParams
    ) -> LayerSetRendererResult:
        layer = layer_for_renderer(ctx.project, request.layer_id)
        renderer = renderer_from_document(layer, request.renderer)
        renderer_type = renderer.type()
        layer.setRenderer(renderer)
        layer.triggerRepaint()
        ctx.project.setDirty(True)
        return LayerSetRendererResult(layer_id=request.layer_id, renderer_type=renderer_type)
