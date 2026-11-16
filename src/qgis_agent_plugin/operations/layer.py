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
哪些分类适用是一张固定清单，不是从 XML 反读出来的——清单就是桌面版那两个样式对话框里的勾选框
（``QgsMapLayerStyleCategoriesModel``）；QML 来自图层自己的原生导出；两份渲染器清单都来自活的
QGIS 注册表，不是这里维护的表。

渲染器设值是这个文件里唯一替换状态的操作。它先把整个原生渲染器建好再碰图层，然后用一次调用装
上去，所以被拒的配置不会给图层留下半套或零套样式。桌面版只在图层有渲染器时才给出符号页
（``QgsVectorLayerProperties``），所以没有渲染器的图层在这里也换不了。

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
something read back out of the XML -- it is the checkboxes of desktop's two style
dialogs (``QgsMapLayerStyleCategoriesModel``); the QML itself comes from the layer's
own native export; and both renderer lists come from the live QGIS registries rather
than a table kept here.

The renderer setter is the one operation here that replaces state.  It builds the
whole native renderer before the layer is touched, then installs it in one call,
so a refused configuration cannot leave the layer with half a style or with none.
Desktop only shows the symbology page when the layer has a renderer
(``QgsVectorLayerProperties``), so a layer without one cannot have one set here.
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
    name: StrictStr = Field(min_length=1)
    # 名字是 provider 的自由文本（空格、斜杠、非 ASCII 都行），但空不是名字。矢量与栅格都能有：
    # 桌面版两个页签都提供子数据集清单（``qgsapplayerhandling.cpp:1363-1381`` 先按类型筛选）。
    # The name is the provider's free text (spaces, slashes, non-ASCII); an empty string is
    # not a name. Vector and raster alike can have one: both tabs of desktop's add dialog
    # offer a subdataset list (``qgsapplayerhandling.cpp:1363-1381`` filters by type first).
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
    changed: StrictBool


def layer_source_uri(provider: str, source: Path, kind: LayerKind, sub_layer: str | None) -> str:
    """provider 为这个源（或它里面的一个子图层）给的 URI，先回答“认得出来吗”。

    桌面版加图层的门槛是 provider 认不认得这个源，不是图层读不读得出来：它先问
    ``providerMetadata.querySublayers``（``qgsapplayerhandling.cpp:1363-1391``），清单为空就在消息栏
    报 "Invalid Data Source" 并且什么也不加；清单不空则把认出的子图层交给
    ``QgsProviderSublayerDetails::toLayer``，即使它的 ``isValid()`` 为假（``:796-815``），
    坏掉的靠图例里的坏图层图标提示。所以这里只查清单，不查 ``isValid()``。

    子图层的 URI 直接取那条 detail 的 ``uri()``——那是 ``toLayer`` 交给
    ``QgsMapLayerFactory`` 的同一个字符串（``qgsprovidersublayerdetails.cpp:22-28``）。
    自己拼是对 provider 规则的猜测：GDAL 把子数据集写成别的形状，实测用
    ``encodeUri({"path", "layerName"})`` 拼出来的 ``…\\dem.tif|name`` 解回来
    ``layerName`` 是 NULL、建出来的栅格无效。子图层也必须在这份清单里：桌面版的选择框只列得出来的
    东西（``qgsprovidersublayersdialog.cpp:215``），点不出一个不存在的名字。

    shortcut: 这里只审调用方点名的文件，不审该文件自己点名的源——本地 ``.vrt`` 里可能装着
    ``/vsicurl/`` 或 WMS 源，不可信 VRT 变成输入时得升级。

    The URI the provider itself gives for this source, or for one sublayer inside it, after
    answering "does the provider recognise it at all".

    Desktop's gate for adding a layer is whether the provider recognises the source, not
    whether the layer loads: it asks ``providerMetadata.querySublayers`` first
    (``qgsapplayerhandling.cpp:1363-1391``), reports "Invalid Data Source" and adds nothing
    when the list is empty, and otherwise hands every recognised sublayer to
    ``QgsProviderSublayerDetails::toLayer`` even when its ``isValid()`` is false
    (``:796-815``), leaving a broken one to the legend's bad-layer icon.  So only the list
    is checked here, not ``isValid()``.

    A sublayer's URI is that detail's own ``uri()`` -- the same string ``toLayer`` hands to
    ``QgsMapLayerFactory`` (``qgsprovidersublayerdetails.cpp:22-28``).  Deriving it by hand
    is a guess about the provider's rule: GDAL spells subdatasets differently, and the
    ``…\\dem.tif|name`` form ``encodeUri({"path", "layerName"})`` produced decodes with a
    NULL ``layerName`` and builds an invalid raster (measured).  A sublayer also has to be
    in that list: desktop's dialog only lists what exists
    (``qgsprovidersublayersdialog.cpp:215``), so a name it never offered is not an answer.

    shortcut: this vets the file the caller named, not the sources that file
    itself names -- a local ``.vrt`` may carry a ``/vsicurl/`` or WMS source
    inside it; upgrade to reading a container's own sources if untrusted VRTs
    become an input.
    """

    from qgis.core import Qgis, QgsProviderMetadata, QgsProviderRegistry

    metadata = QgsProviderRegistry.instance().providerMetadata(provider)
    recognised: list[Any] | None = None
    if metadata.capabilities() & QgsProviderMetadata.QuerySublayers:
        wanted = Qgis.LayerType.Vector if kind == "vector" else Qgis.LayerType.Raster
        recognised = [
            detail for detail in metadata.querySublayers(str(source)) if detail.type() == wanted
        ]
        if not recognised:
            raise FrameworkError(INVALID_LAYER, "the layer source could not be loaded")
    if sub_layer is None:
        return str(source)
    if recognised is None:
        # 不能列子图层的 provider 也给不出子图层的 URI，桌面版那条分支同样不提供子数据集选择。
        # A provider that cannot list sublayers cannot name one either; desktop's branch for
        # it offers no subdataset choice at all (``:1451-1469``).
        raise FrameworkError(INVALID_LAYER, "the provider cannot name a sub-layer")
    for detail in recognised:
        if detail.name() == sub_layer:
            return detail.uri()
    raise FrameworkError(INVALID_LAYER, "the layer source has no such sub-layer")


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
        uri = layer_source_uri(provider, source, request.kind, request.sub_layer)
        layer = layer_class(uri, request.name, provider)
        # 不查 isValid()：桌面版把 provider 认出的子图层直接加进图例，读不出来的靠坏图层图标提示
        # （qgsapplayerhandling.cpp:796-815 与 :1383-1390 的分工）。
        # No isValid() check: desktop adds every recognised sublayer to the legend and leaves a
        # broken one to the bad-layer icon. The two source lines above are that split.
        #
        # 永远进图例：桌面版的 addToLegend=false 是“先进工程、随后进分组”，图层仍在图例里
        # （qgsapplayerhandling.cpp:802-808），所以这里没有那个参数。
        # Always into the legend: desktop's addToLegend=false is "into the project, then into a
        # group", and the layer is still in the legend (qgsapplayerhandling.cpp:802-808), so
        # there is no such parameter here.
        ctx.project.addMapLayer(layer)
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
    """切一个图层在图例里的勾选；勾上时连祖先分组一起勾，取消只动这一个节点。

    勾选框不是“只改自己”：桌面版对一个叶子节点打勾时会连它的祖先一起勾
    （``QgsLayerTreeModel.setData`` 对没有子节点的节点调
    ``setItemVisibilityCheckedParentRecursive``，``qgslayertreemodel.cpp:401-405``），否则勾了也
    看不见；取消只写节点自己（``:413-416``）。

    Toggle one layer's legend check; checking also checks the ancestor groups, unchecking
    touches only this node.  The checkbox is not "this node only": desktop ticks a leaf's
    ancestors along with it (``setData`` calls ``setItemVisibilityCheckedParentRecursive``
    for a childless node, ``qgslayertreemodel.cpp:401-405``), or the tick would show
    nothing; unchecking writes the node alone (``:413-416``).
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
        # 勾上要连祖先，所以“已经对了”不是“节点已勾”而是“真的可见”/ checking must reach the
        # ancestors, so "already there" means "already visible", not "node checked".
        changed = bool(node.itemVisibilityChecked()) != request.visible or (
            request.visible and not node.isVisible()
        )
        if changed:
            if request.visible:
                node.setItemVisibilityCheckedParentRecursive(request.visible)
            else:
                node.setItemVisibilityChecked(request.visible)
        return LayerSetVisibilityResult(
            layer_id=request.layer_id,
            visible=bool(node.itemVisibilityChecked()),
            effective_visible=bool(node.isVisible()),
            changed=changed,
        )


class LayerSumNumericFieldParams(Params):
    layer_id: StrictStr = Field(min_length=1)
    field_name: StrictStr = Field(min_length=1)


class LayerSumNumericFieldResult(Result):
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
    ``QgsTask.setDependentLayers``: removing the layer then cancels the task, as
    QGIS does for any dependent task.
    """

    name = "layer.sum_numeric_field"
    description = "Sum one numeric field of a vector layer over a copied snapshot"
    input_model = LayerSumNumericFieldParams
    output_model = LayerSumNumericFieldResult

    def prepare_main(
        self, ctx: OperationContext, request: LayerSumNumericFieldParams
    ) -> FieldValues:
        from qgis.core import NULL, QgsFeatureRequest, QgsVectorLayer

        layer = project_layer(ctx.project, request.layer_id)
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            raise FrameworkError(INVALID_LAYER, "the layer is not a valid vector layer")
        fields = layer.fields()
        index = fields.indexFromName(request.field_name)
        if index < 0:
            raise FrameworkError(INVALID_FIELD_TYPE, "the layer has no such field")
        if not fields.at(index).isNumeric():
            raise FrameworkError(INVALID_FIELD_TYPE, "the field is not integer or floating point")

        # 求和只读一个字段、不要几何：请求裁剪到那一列，主线程不为别的列和每个几何付拷贝费。
        # The sum reads one column and no geometry: the request is trimmed to that column, so the
        # main thread does not pay for the other columns or for any geometry.
        read = (
            QgsFeatureRequest()
            .setFlags(QgsFeatureRequest.NoGeometry)
            .setSubsetOfAttributes([index])
        )
        # 跟随桌面版统计面板的选择集规则：有选择集时只算选中的，没有时算全部；面板那个开关
        # 就是这件事（qgsstatisticalsummarydockwidget.cpp:210 读它、:218 交给
        # getValuesIterator，后者在 qgsvectorlayerutils.cpp:73-80 选 getSelectedFeatures
        # 还是 getFeatures）。没有新参数：那是图层的状态，不是调用方的一个问句。
        # Follow desktop's statistics-panel selection rule: with a selection, sum the selected
        # features only; without one, sum them all. That is exactly what the panel's checkbox
        # does (it is read at qgsstatisticalsummarydockwidget.cpp:210, handed to
        # getValuesIterator at :218, which picks getSelectedFeatures or getFeatures at
        # qgsvectorlayerutils.cpp:73-80). No new parameter: this is layer state, not a question
        # for the caller.
        selected_only = bool(layer.selectedFeatureCount())
        features = layer.getSelectedFeatures(read) if selected_only else layer.getFeatures(read)
        values: list[float] = []
        skipped = 0
        for feature in features:
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
            selected_only=selected_only,
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
    "MapTips",
    "Rendering",
    "CustomProperties",
    "Temporal",
    "Elevation",
    "AttributeTable",
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
    """id 背后的图层，前提它是一个有效的矢量或栅格。

    样式读写只对这两种形体有定义，所以别人的图层类型是 ``INVALID_LAYER`` 而不是空答案：「没有分类」
    和「不适用」是两个结果。几何不是条件：一个没有几何的矢量表（属性表）也有字段、表单、属性表
    设置这些分类，也能导出 QML——桌面版对它给出的正是整张 18 项清单。

    Style reads and writes are defined only for those two shapes, so somebody else's
    layer type is ``INVALID_LAYER`` rather than an empty answer: "no categories" and
    "not applicable" are different results.  Geometry is not a condition: a
    geometry-less vector table still has fields, forms and attribute-table settings to
    export, and desktop's two style dialogs hand it the whole 18-item list.
    """

    layer = project_layer(project, layer_id)
    if not layer.isValid() or layer_kind(layer) == "other":
        raise FrameworkError(INVALID_LAYER, "the layer is not a valid vector or raster layer")
    return layer


def layer_for_renderer(project: Any, layer_id: str) -> Any:
    """一个能读、能换渲染器的图层：桌面版给它符号页的那个。

    矢量属性对话框只看一件事：图层有没有渲染器；没有的话整个符号页被禁用
    （``qgsvectorlayerproperties.cpp:1534-1549``：`if ( mLayer->renderer() ) … else
    mOptsPage_Style->setEnabled( false )`），里面那个渲染器下拉框、当前渲染器和“换上这个渲染器”的
    按钮就都不存在。一个没有几何的矢量表就是这样（实测 ``renderer()`` 是 ``None``）。所以没有
    渲染器的图层在这里也是 ``INVALID_LAYER``：同一个前置条件，同一句话。

    栅格那一半没有对应的页面级守卫（``qgsrasterlayerproperties.cpp`` 只在 ``:1095`` 的槽里早返回），
    但它对有效栅格不可达：有效栅格总有渲染器。

    A layer whose renderer can be read or replaced: the one desktop gives a symbology
    page to.  The vector properties dialog looks at one thing: whether the layer has a
    renderer.  Without one the whole page is disabled
    (``qgsvectorlayerproperties.cpp:1534-1549``: `if ( mLayer->renderer() ) … else
    mOptsPage_Style->setEnabled( false )`), and its renderer combo box, its current
    renderer and its install button do not exist.  A geometry-less vector table is
    exactly that case (measured: ``renderer()`` is ``None``).  So a layer without a
    renderer is ``INVALID_LAYER`` here too: one precondition, one sentence.

    The raster half has no page-level guard of that kind
    (``qgsrasterlayerproperties.cpp`` only returns early inside the slot at ``:1095``), but
    it is unreachable for a valid raster: a valid raster always has a renderer.
    """

    layer = layer_for_style(project, layer_id)
    if layer.renderer() is None:
        raise FrameworkError(INVALID_LAYER, "the layer has no renderer")
    return layer


def style_categories(layer: Any) -> tuple[StyleCategoryName, ...]:
    """适用一个已经审过的图层的分类，按固定顺序。

    The categories that apply to an already-vetted layer, in the fixed order.
    """

    return RASTER_STYLE_CATEGORIES if layer_kind(layer) == "raster" else VECTOR_STYLE_CATEGORIES


def style_category_flag(names: Sequence[str]) -> Any:
    """恰好点出 ``names`` 的原生位标志，按给定的顺序。

    空序列对应原生那个“一个都不选”的位标志：保存样式对话框有 Deselect All
    （``qgsmaplayersavestyledialog.cpp:82``），导出层接受零分类（实测返回空消息、163 字节文档）。

    The native bit flag naming exactly ``names``, in their given order.  An empty
    sequence is the native "nothing selected" flag: the save-style dialog has
    Deselect All (``qgsmaplayersavestyledialog.cpp:82``) and the exporter takes zero
    categories (measured: an empty message and a 163-byte document).
    """

    from qgis.core import QgsMapLayer

    if not names:
        return QgsMapLayer.StyleCategories()
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


def raster_renderer_types(layer: Any) -> list[str]:
    """栅格符号页下拉框里的那些名字：注册表清单再按图层的 ``rasterType()`` 过滤。

    桌面版在 ``renderersList()`` 之后还有一层过滤：普通栅格不列 ``singlebandcolordata``，而
    ``SingleBandColorData`` 栅格只列它那一个（``qgsrasterlayerproperties.cpp:443-455``）。实测：
    单波段、三波段、调色板三类栅格都是 7 个名字，注册表是 8 个。

    The names desktop puts in a raster layer's renderer combo: the registry list filtered
    once more by the layer's own ``rasterType()``.  Desktop applies that filter after
    ``renderersList()``: an ordinary raster does not list ``singlebandcolordata``, and a
    ``SingleBandColorData`` raster lists only that one
    (``qgsrasterlayerproperties.cpp:443-455``).  Measured: one-band, three-band and
    paletted rasters each list 7 names against a registry of 8.
    """

    from qgis.core import Qgis, QgsRasterRendererRegistry

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
        from qgis.core import QgsApplication

        layer = layer_for_renderer(ctx.project, request.layer_id)
        if layer_kind(layer) == "raster":
            # 3.44 里 ``QgsApplication.rasterRendererRegistry()`` 是 SIP_SKIP / it is SIP_SKIP
            # in 3.44, 而且类没绑 ``instance()`` / and the class does not bind instance(),
            # 所以自建一个注册表对象是拿到栅格清单的唯一 Python 路径 / so a registry object is
            # the only Python path to the raster list, 也是这个构建给的全部 API / and all this
            # 构建给的全部 / build exposes.
            renderer_types = raster_renderer_types(layer)
        else:
            renderer_types = QgsApplication.rendererRegistry().renderersList(layer)
        return LayerListAvailableRenderersResult(renderer_types=sorted(set(renderer_types)))


class LayerGetRenderer(
    BaseQuery[LayerGetRendererParams, LayerGetRendererResult],
    MainThreadOperationMixin[LayerGetRendererParams, LayerGetRendererResult],
):
    """图层当前那个渲染器的类型名。

    The type name of the renderer the layer currently has.
    """

    name = "layer.get_renderer"
    description = "Read the type name of one layer's current renderer"
    input_model = LayerGetRendererParams
    output_model = LayerGetRendererResult

    def execute_main(
        self, ctx: OperationContext, request: LayerGetRendererParams
    ) -> LayerGetRendererResult:
        layer = layer_for_renderer(ctx.project, request.layer_id)
        return LayerGetRendererResult(
            layer_id=request.layer_id,
            renderer_type=layer.renderer().type(),
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

    # 不画符号不需要几何：桌面版的 "No symbols" 对任何有渲染器的图层都能选，几何未知也一样。
    # Not drawing a symbol needs no geometry: desktop's "No symbols" is available on every
    # layer that has a renderer, unknown geometry included.
    if isinstance(config, NullSymbolRendererConfig):
        return QgsNullSymbolRenderer()
    expected = expected_symbol_type(layer)
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
        layer = layer_for_renderer(ctx.project, request.layer_id)
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
