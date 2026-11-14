"""图层操作：载入本地文件、列出与查看图层、切开关可见性。

除 ``layer.sum_numeric_field`` 外，这里每个操作都跑在主线程并经 ``ctx.project``，读的是引擎持有的
工程，不是全局单例。QGIS 图层从不离开建它的方法：出去的只有 id、名字、provider、CRS 和两个
可见性标志，因为这些是调用方能据以行动的値，也是序列化后仍然存活的那些。

字段求和是唯一的后台操作，所以它的读法分散在三个阶段：数字在主线程拷出来，在后台在纯 float 元组
上求和，回来时再和工程代数核对一次。

``visible`` 与 ``effective_visible`` 故意分成两个字段。一个图层自己的勾选可以为真而父分组把它
藏着，两件事报成一个数会让一次切换看起来像失败。

样式读取（``layer.list_style_categories``、``layer.get_qml``、
``layer.list_available_renderers``、``layer.get_renderer``）描述图层已经带着什么，不改它。
哪些分类适用是一张固定清单，不是从 XML 反读出来的；QML 来自图层自己的原生导出；两份渲染器清单
都来自活的 QGIS 注册表，不是这里维护的表。

渲染器设值是这个文件里唯一替换状态的操作。它先把整个原生渲染器建好再碰图层，然后用一次调用装
上去，所以被拒的配置不会给图层留下半套或零套样式。

Every operation here but ``layer.sum_numeric_field`` runs on the main thread and
works through ``ctx.project``, so it reads the project the engine holds rather than
a global singleton.  A QGIS layer never leaves the method that built it: only its
id, name, provider, CRS and the two visibility flags travel out, because those are
the values a caller can act on and the only ones that survive serialisation.

The field sum is the one background operation, so its reading is split across the
three stages: the numbers are copied out on the main thread, summed off it over a
tuple of plain floats, and checked against the project generation on the way back.

``visible`` and ``effective_visible`` are deliberately two fields.  A layer's own
check state can be true while its parent group hides it, and reporting one number
for both would make a toggle look like it failed.

The style reads (``layer.list_style_categories``, ``layer.get_qml``,
``layer.list_available_renderers``, ``layer.get_renderer``) describe what a layer
already carries without changing it.  Which categories apply is a fixed list, not
something read back out of the XML; the QML itself comes from the layer's own native
export; and both renderer lists come from the live QGIS registries rather than a
table kept here.

The renderer setter is the one operation here that replaces state.  It builds the
whole native renderer before the layer is touched, then installs it in one call,
so a refused configuration cannot leave the layer with half a style or with none.
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
    Result,
)
from ..core.security import absolute_path

# 调用方可以请求载入什么 / what a caller may ask to load; ``other`` 只在读的那一侧 /
# 只在读的一侧 / ``other`` exists only on the reading side.
LayerKind = Literal["vector", "raster"]
LayerSummaryKind = Literal["vector", "raster", "other"]


class LayerAddParams(Params):
    """一个要载入的本地文件，名字由调用方定。

    One local file to load, under a name the caller chooses.
    """

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
    changed: StrictBool


def layer_source_uri(source: Path, sub_layer: str | None) -> str:
    """返回一个数据源的 provider URI。

    调用方给的字符串只经过绝对路径检查，是不是本地文件由 provider 决定——``/vsicurl/`` 与
    WMS 地址都是合法的源。子图层仍然要靠 ogr provider 自己的编码器：它的 URI 怎么把路径和图层名
    拼在一起、每一段怎么转义，是 provider 的规则——自己照 ``path|layername=name`` 重建形状，就是
    对那条规则的猜测。

    shortcut: 这里只审调用方点名的文件，不审该文件自己点名的源——本地 ``.vrt`` 里可能装着
    ``/vsicurl/`` 或 WMS 源，不可信 VRT 变成输入时得升级。

    The caller's string has been checked for being absolute and nothing more; whether
    it is a local file is the provider's decision -- ``/vsicurl/`` and a WMS URL are both
    legitimate sources.  A sub-layer still needs the ogr provider's own encoder: how its
    URI joins a path and a layer name, and how it escapes each part, is the provider's
    rule -- re-deriving the ``path|layername=name`` shape by hand is a guess about that
    rule.

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
    """给一个已注册图层分类；QGIS 能装的东西都列出来，不拒。

    Classify a registered layer; anything QGIS can hold is listed, not refused.
    """

    from qgis.core import QgsRasterLayer, QgsVectorLayer

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

    没有节点的图层没有自己的可见性，所以两个标志都是 ``None``，而不是假称它被隐藏了。

    A layer with no node has no visibility of its own, so both flags are ``None``
    rather than a false claim that it is hidden.
    """

    node = layer_tree_node(project, layer_id)
    if node is None:
        return False, None, None
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
        from qgis.core import QgsRasterLayer, QgsVectorLayer

        source = absolute_path(request.source_path)
        # provider 是声明类型算出来的：调用方点不了它 / the provider is a function of the
        # declared kind, 所以没人能自己指定一个 / so a caller cannot name one.
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
    """每个已注册图层，按 id 排序，所以输出是稳定的。

    Every registered layer, sorted by id so the output is stable.
    """

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
    """切一个图层自己的勾选状态；父分组保持原样。

    Toggle one layer's own check state; a parent group stays as it is.
    """

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


class SumNumericFieldResult(Result):
    """一份快照的和，不是任务结束时图层上的值。

    shortcut: 值按 float64 求和，所以值超过 2**53 的 QInt64 字段在 ``total`` 里会丢精度；
    需要精确整数和时再补一条整数路径。

    The sum of a snapshot, which is not the layer's value when the task ends.

    shortcut: the values are summed as float64, so a QInt64 field with values above
    2**53 loses precision in ``total``; add an integer path when an exact integer
    sum is needed.
    """

    layer_id: StrictStr
    field_name: StrictStr
    count: int
    skipped_null: int
    total: float
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
    project_epoch: int


@dataclass(frozen=True)
class FieldSum:
    """后台算出来的东西，连同它据以计算的工程代数。

    What the worker computed, with the generation it was computed from.
    """

    count: int
    total: float
    skipped_null: int
    project_epoch: int


class LayerSumNumericField(
    BaseQuery[SumNumericFieldParams, SumNumericFieldResult],
    BackgroundOperationMixin[SumNumericFieldParams, SumNumericFieldResult, FieldValues, FieldSum],
):
    """在主线程之外、在一份快照上求一个数值字段的和。

    任务声明了 ``dependent_layer_ids``，于是适配层能把它交给 ``QgsTask.setDependentLayers``：
    移除图层就会取消任务，就像 QGIS 对任何依赖任务做的那样。

    The task declares ``dependent_layer_ids`` so the adapter can hand it to
    ``QgsTask.setDependentLayers``: removing the layer then cancels the task, as
    QGIS does for any dependent task.
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
                # 从不静悄悄截断 / never truncate quietly: 一个看起来完整的部分和比一次拒绝更糟 /
                # 部分和比拒绝更糟 / a partial sum that looks complete is worse than a refusal.
                raise FrameworkError(
                    SNAPSHOT_LIMIT_EXCEEDED, f"the layer has more than {limit} features"
                )
            value = feature[index]
            # QGIS 把空属性交回它自己的 NULL 单例（一个 QVariant）/ QGIS hands a null
            # attribute back as its own NULL singleton (a QVariant), 有些构建则返回 None /
            # and some builds return None instead; 两者都算跳过的空值 / both are the
            # 跳过的空值 / "skipped null" case.
            if value is None or value == NULL:
                skipped += 1
                continue
            number = float(value)
            if not math.isfinite(number):
                # ``total`` 是 NaN 或 Infinity 的记录 / a record whose ``total`` is NaN or
                # Infinity 不是 RFC 8259 解析器能读的 JSON / is not JSON an RFC 8259 parser
                # reads（实测 ``json.dumps(..., allow_nan=False)`` 会报错 / measured: it
                # raises）, 所以求和宁可直接拒 / so the sum refuses rather than deliver a
                # 不交解不开的结果 / result the caller cannot parse.
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
                    # 在这里停是取消的协作那一半 / stopping here is the cooperative half of
                    # the cancel; 适配层把部分运行变成 CANCELLED 并丢掉数字 / the adapter
                    # 变成 CANCELLED / turns the partial run into CANCELLED and drops them.
                    break
                control.set_progress(100.0 * position / len(values))
            total += value
            count += 1
        if not math.isfinite(total):
            # 两个有限值也可能加出 Infinity（1e308 + 1e308）/ two finite values can still
            # add up to Infinity (1e308 + 1e308), 而 ``total`` 是 Infinity 的记录不是
            # RFC 8259 解析器能读的 / and a record whose ``total`` is Infinity is not JSON
            # 能读的 JSON / an RFC 8259 parser reads（实测 / measured: ``json.dumps(...)
            # allow_nan=False)`` 会报错 / raises）, 所以拒掉不交 / so it is refused rather
            # 不交出去 / than delivered.
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


# --- 读样式：分类、QML 与渲染器类型 / reading the style: categories, QML, renderers ---

# ``QgsMapLayer.StyleCategory`` 的 18 个名字，按 QGIS 声明它们的顺序 / the 18
# ``QgsMapLayer.StyleCategory`` names in the order QGIS declares them, 以及
# ``QgsRasterLayer`` 真正导出的那部分 / and the subset ``QgsRasterLayer`` really exports.
# 这里它们是名字不是标志 / they are names here, not flags: 位标志只为一次请求才建 /
# the bit flag is only built for a request, 所以本模块没有 QGIS 也能加载 / so this module
# 无 QGIS 也能加载 / still loads without QGIS.
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
    """id 背后的图层，没有就是 ``LAYER_NOT_FOUND``；每个读取都从这里开始。

    The layer behind an id, or ``LAYER_NOT_FOUND``; every reader starts here.
    """

    layer = project.mapLayer(layer_id)
    if layer is None:
        raise FrameworkError(LAYER_NOT_FOUND, "layer does not exist")
    return layer


def layer_for_style(project: Any, layer_id: str) -> Any:
    """id 背后的图层，前提它是一个有效的空间矢量或栅格。

    整个读取侧只对这两种形体有定义。一个矢量表、一个无效图层或别人的图层类型都是
    ``INVALID_LAYER`` 而不是空答案，因为「没有分类」和「不适用」是两个不同结果。

    The whole read side is defined only for those two shapes.  A vector table, an
    invalid layer or somebody else's layer type is ``INVALID_LAYER`` rather than
    an empty answer, because "no categories" and "not applicable" are different
    results.
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
    """适用一个已经审过的图层的分类，按固定顺序。

    The categories that apply to an already-vetted layer, in the fixed order.
    """

    return RASTER_STYLE_CATEGORIES if layer_kind(layer) == "raster" else VECTOR_STYLE_CATEGORIES


def style_category_flag(names: Sequence[str]) -> Any:
    """恰好点出 ``names`` 的原生位标志，按给定的顺序。

    The native bit flag naming exactly ``names``, in their given order.
    """

    from qgis.core import QgsMapLayer

    flag = getattr(QgsMapLayer, names[0])
    for name in names[1:]:
        flag = flag | getattr(QgsMapLayer, name)
    return flag


class LayerListStyleCategoriesParams(Params):
    layer_id: StrictStr = Field(min_length=1)


class LayerListStyleCategoriesResult(Result):
    layer_id: StrictStr
    categories: list[StyleCategoryName]


class LayerGetQmlParams(Params):
    layer_id: StrictStr = Field(min_length=1)
    categories: Annotated[list[StyleCategoryName] | None, Field(min_length=1)] = None


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
            # 3.44 的绑定返回导出错误信息（成功时为空）并填好文档 / 3.44's binding returns
            # the export's error message (empty on success) and fills the document; 它不返回
            # ``(message, ok)`` / it does not return ``(message, ok)``——这是对着本机安装的
            # ``QgsMapLayer.exportNamedStyle`` 核过的 / checked against this install's
            # 本安装的 ``QgsMapLayer.exportNamedStyle`` / own binding of that method.
            message = layer.exportNamedStyle(
                document, QgsReadWriteContext(), style_category_flag(chosen)
            )
        except Exception as error:  # noqa: BLE001 - any exporter failure is one refusal
            raise FrameworkError(INTERNAL_ERROR, "the layer style could not be exported") from error
        if message:
            # 原生消息可能点出文件名，所以它不进记录 / the native message may name a file, so
            # 不进记录 / it stays out of the record.
            raise FrameworkError(INTERNAL_ERROR, "the layer style could not be exported")
        return LayerGetQmlResult(layer_id=request.layer_id, qml=document.toString())


class LayerListAvailableRenderers(
    BaseQuery[LayerListAvailableRenderersParams, LayerListAvailableRenderersResult],
    MainThreadOperationMixin[LayerListAvailableRenderersParams, LayerListAvailableRenderersResult],
):
    """活的 QGIS 注册表为这个图层提供的渲染器类型名。

    The renderer type names the live QGIS registries offer for this layer.
    """

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
            # 3.44 里 ``QgsApplication.rasterRendererRegistry()`` 是 SIP_SKIP / it is SIP_SKIP
            # in 3.44, 而且类没绑 ``instance()`` / and the class does not bind instance(),
            # 所以自建一个注册表对象是拿到栅格清单的唯一 Python 路径 / so a registry object is
            # the only Python path to the raster list, 也是这个构建给的全部 API / and all this
            # 构建给的全部 / build exposes.
            renderer_types = QgsRasterRendererRegistry().renderersList()
        else:
            renderer_types = QgsApplication.rendererRegistry().renderersList(layer)
        return LayerListAvailableRenderersResult(renderer_types=sorted(set(renderer_types)))


class LayerGetRenderer(
    BaseQuery[LayerGetRendererParams, LayerGetRendererResult],
    MainThreadOperationMixin[LayerGetRendererParams, LayerGetRendererResult],
):
    """图层当前那个渲染器的类型名，有的话。

    The type name of the renderer the layer currently has, if any.
    """

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


# --- 设渲染器：那几套结构化配置 / setting the renderer: the structured configurations ---

# 颜色是六位 RGB 十六进制、无 alpha，长度单位是毫米 / a colour is six RGB hex digits
# with no alpha and a length is millimetres: 这是简单符号 API 接受的唯二形状 / those are
# 唯二形状 / the only shapes the simple-symbol API here accepts.
type ColorHex = Annotated[StrictStr, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]
type FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
type PositiveMm = Annotated[float, Field(gt=0, allow_inf_nan=False)]
type Band = Annotated[StrictInt, Field(ge=1)]


class MarkerSymbolConfig(BaseModel):
    """一个圆点标记，是这一版唯一能配的点符号。

    A round marker, the only point symbol this version can configure.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["marker"]
    color: ColorHex
    size_mm: PositiveMm = 2.0


class LineSymbolConfig(BaseModel):
    """一条实线：没有虚线样式，没有偏移。

    A solid line: no dash pattern, no offset.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["line"]
    color: ColorHex
    width_mm: PositiveMm = 0.3


class FillSymbolConfig(BaseModel):
    """一块平面填充加一圈细边；轮廓不能隐藏。

    A flat fill with a simple outline; the outline cannot be hidden.
    """

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
    """不画任何要素符号；它和隐藏图层不是一回事。

    Draws no feature symbol; it is not the same as hiding the layer.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["nullSymbol"]


class SingleSymbolRendererConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["singleSymbol"]
    symbol: SimpleSymbolConfig


class CategoryConfig(BaseModel):
    """关键字段的一个取值和画它用的符号。

    One value of the key field and the symbol it is drawn with.
    """

    model_config = ConfigDict(extra="forbid")

    value: str | int | FiniteFloat | bool | None
    label: StrictStr = Field(min_length=1)
    symbol: SimpleSymbolConfig


class CategorizedRendererConfig(BaseModel):
    """分类值全由调用方给出：不从数据里推任何东西。

    The caller supplies every category: nothing is derived from the data.
    """

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
    """每个断点全由调用方给出；QGIS 自己不分类。

    The caller supplies every breakpoint; QGIS classifies nothing itself.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["graduatedSymbol"]
    field: StrictStr = Field(min_length=1)
    ranges: list[RangeConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _ranges_are_ordered(self) -> GraduatedRendererConfig:
        # 相邻区间可以共用一个端点；留空隙是调用方的选择 / adjacent ranges may share an
        # endpoint, 留不留空隙由调用方定 / gaps are the caller's choice.
        for previous, current in zip(self.ranges, self.ranges[1:], strict=False):
            if current.lower < previous.upper:
                raise ValueError("ranges must be ordered by lower and must not overlap")
        return self


class RuleConfig(BaseModel):
    """一条平面规则：一个筛选表达式或唯一的 ELSE，加上它的符号。

    A flat rule: a filter expression or the single ELSE, plus its symbol.
    """

    model_config = ConfigDict(extra="forbid")

    label: StrictStr = Field(min_length=1)
    filter: StrictStr | None = None
    else_rule: StrictBool = False
    symbol: SimpleSymbolConfig


class RuleRendererConfig(BaseModel):
    """规则保留调用方的顺序；嵌套与比例尺限制不在范围内。

    Rules keep the caller's order; nesting and scale limits are out of scope.
    """

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
    """同一个波段可以点两个或三个通道。

    The same band may name two or three channels.
    """

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

# 一个联合，所以榜上那九种之外的类型被模型自己拒掉 / one union, so a type outside the nine
# 九种之外都被拒 / listed is refused by the model itself.
type RendererConfig = Annotated[
    VectorRendererConfig | RasterRendererConfig,
    Field(discriminator="type"),
]

# 每种声明的插值对应的 shader 斜坡类型 / the shader ramp type per declared interpolation:
# 值就是 ``QgsColorRampShader.Type`` 成员的名字 / the values are the names of
# 成员名 / ``QgsColorRampShader.Type`` members.
PSEUDO_RAMP_TYPES = {"linear": "Interpolated", "discrete": "Discrete", "exact": "Exact"}


class LayerSetRendererParams(Params):
    layer_id: StrictStr = Field(min_length=1)
    renderer: RendererConfig


class LayerSetRendererResult(Result):
    layer_id: StrictStr
    renderer_type: StrictStr


def expected_symbol_type(layer: Any) -> Literal["marker", "line", "fill"]:
    """一个图层的几何接受的简单符号；Multi* 报它的基础类型。

    The simple symbol a layer's geometry takes; Multi* reports its base type.
    """

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
    """只用声明的颜色和大小建一个原生符号。

    One native symbol from the declared colour and size alone.
    """

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
    """图层自己的字段，所以一个值是对着真实类型核的。

    The layer's own field, so a value is checked against its real type.
    """

    fields = layer.fields()
    index = fields.indexFromName(name)
    if index < 0:
        raise FrameworkError(INVALID_FIELD_TYPE, "the layer has no such field")
    return fields.at(index)


def category_key(field: Any, value: Any) -> tuple[tuple[str, Any], Any]:
    """两个分类拿来比较的键，以及渲染器存下的那个值。

    整数和浮点数算同一个数值分类。其它每个值先转成字段自己的类型：字段装不下的值被拒，而不是变成
    一个永远匹配不上的分类；一个值的两种写法（``True`` 与 ``"true"``）就是同一个分类。整数
    字段只收自己范围内的整数值，并按整数存——和它自己的值同形。Qt 是按数值比较分类值的
    （``2`` 和 ``2.0`` 都能匹配要素值 2），所以存的形状关乎字段装得下什么，不关乎匹配。

    An integer and a float are the same numeric category.  Every other value is
    converted to the field's own type first: a value the field cannot hold is
    refused instead of becoming a category that can never match, and two spellings
    of one value (``True`` and ``"true"``) are one category.  An integer field takes
    only whole values inside its own range, and stores them as integers -- the same
    shape its own values have.  Qt compares category values numerically (``2`` and
    ``2.0`` both match a feature value of 2), so the stored shape is about the value
    the field can hold, not about the match.
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
            # 浮点数装不下的整数在这里就不算有限值 / an integer the float cannot hold is not
            # finite for this purpose, 而且它不能进去重键或原生分类 / and it must not reach
            # 去重键或原生分类 / either the dedupe key or the native category.
            raise FrameworkError(
                INVALID_INPUT, "a category value does not fit the field type"
            ) from None
        if field.type() in (QVariant.Int, QVariant.LongLong):
            # 整数字段上的小数永远匹配不到要素 / a fraction on an integer field can never
            # match a feature; 整数值按整数存与去重 / a whole value is stored and deduped as
            # an integer, 所以 2 和 2.0 是一个分类 / so 2 and 2.0 are one category, 而 float
            # 分不开的两个 int64 值仍是两个 / while two int64 values a float cannot tell
            # apart stay two. UInt/ULongLong 不在此列 / are left out: 这个构建没有 provider
            # 生产这种字段 / no provider in this build makes such a field.
            if not number.is_integer():
                raise FrameworkError(INVALID_INPUT, "a category value does not fit the field type")
            whole = int(value)
            # 超出 int32/int64 的整数不是字段装得下的值 / an integer outside int32/int64 is
            # not a value the field can hold（内存 provider 会拒这种属性，PyQt 对 int64 会
            # 溢出 / the memory provider refuses such an attribute and PyQt overflows for
            # int64）, 所以它的分类永远匹配不上 / so its category would never match.
            # ``QgsField.convertCompatible`` 对 LongLong 溢出是包一圈而不是拒（2**63 -> -2**63，
            # 实测）/ it wraps a LongLong overflow instead of refusing it (2**63 -> -2**63,
            # measured), 所以这里得自己定界 / hence the bounds.
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
    # 转换后的值才是字段装得下的 / the converted value -- a QDate for a date field, a bool
    # 日期字段的 QDate、布尔字段的 bool / for a boolean one -- is what the field can hold,
    # 所以它同时是去重键和原生值 / so it is both the dedupe key and the native value.
    converted = variant.value()
    return ("value", converted), converted


def raster_band(layer: Any, band: int) -> int:
    """波段从 1 开始，且只有图层自己的波段可以点。

    Bands are 1-based and only the layer's own bands are addressable.
    """

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
    """完整的原生渲染器；这里不碰图层。

    The complete native renderer; the layer is not touched here.
    """

    if isinstance(config, RASTER_RENDERER_CONFIGS):
        if layer_kind(layer) != "raster":
            raise FrameworkError(INVALID_LAYER, "the renderer does not apply to this layer")
        return raster_renderer(layer, config)
    if layer_kind(layer) != "vector":
        raise FrameworkError(INVALID_LAYER, "the renderer does not apply to this layer")
    return vector_renderer(layer, config)


def vector_renderer(layer: Any, config: VectorRendererConfig) -> Any:
    """建五个矢量渲染器之一，符号一并建好。

    Build one of the five vector renderers, symbols included.
    """

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

    # 规则的筛选表达式只查语法：它的结果在这里从不求值 / a rule's filter is only checked
    # for syntax: 求值留给 QGIS / its result is never evaluated here.
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
    """从图层自己的 provider 建四个栅格渲染器之一。

    Build one of the four raster renderers from the layer's own provider.
    """

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
        # 对比增强拿波段自己的数据类型 / the enhancement takes the band's own data type,
        # 否则浮点波段的值在拉伸之前就被剪到 8 位范围 / or a float band's values would be
        # 剪到 8 位 / clipped to an 8-bit range before the stretch sees them.
        enhancement = QgsContrastEnhancement(provider.dataType(band))
        # 下面的算法设置只建一次查找表 / the algorithm setter below builds the lookup table
        # once; 这两个值只存边界 / the two values only store their bounds.
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
    """用一次调用换掉一个图层的整个渲染器。

    Replace one layer's whole renderer in a single call.

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
            # 原生消息可能点出字段或文件名 / the native message may name a field or a file,
            # 所以它不进记录 / so it stays out of the record.
            raise FrameworkError(INTERNAL_ERROR, "the renderer could not be set") from error
        return LayerSetRendererResult(layer_id=request.layer_id, renderer_type=renderer_type)
