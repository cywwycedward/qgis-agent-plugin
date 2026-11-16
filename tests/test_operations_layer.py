"""用假 QGIS 驱动 ``layer.*`` 操作。

真实的 provider 与图层树由 QGIS 侧的验收跑覆盖；这里只问契约是否成立——哪条路径被拒、
回的是哪个错误码、什么 URI 进了图层构造函数，以及图层自己的勾选状态是否和实际可见性
分开。

The layer operations against a fake QGIS.  The real provider and the real layer tree are
exercised by the QGIS acceptance run; here the question is whether the contract holds --
which path is refused, which error code comes back, which URI reaches the layer
constructor, and whether a layer's own check state stays apart from its effective
visibility.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from qgis_agent_plugin.core.config import Settings
from qgis_agent_plugin.core.contracts import OperationContext
from qgis_agent_plugin.core.models import (
    INTERNAL_ERROR,
    INVALID_FIELD_TYPE,
    INVALID_INPUT,
    INVALID_LAYER,
    LAYER_NOT_FOUND,
    LAYER_NOT_IN_TREE,
    FrameworkError,
)
from qgis_agent_plugin.core.registry import OperationRegistry
from qgis_agent_plugin.operations import layer as ops

# 固定的样式分类名，假件按这个顺序给每个名字一位。
# 样式测试断言返回的正好是这些名字。
# The fixed categories, one bit each.
# The style tests assert exactly these names come back.
VECTOR_CATEGORIES = (
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

RASTER_CATEGORIES = (
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

# --- 假 QGIS 层 / the fake QGIS ---------------------------------------------


class FakeSublayerDetails:
    """``QgsProviderSublayerDetails`` 的那三个读法：名字、URI、类型。

    The three reads of ``QgsProviderSublayerDetails``: name, URI and layer type.
    """

    def __init__(self, name: str, uri: str, layer_type: int) -> None:
        self._name = name
        self._uri = uri
        self._type = layer_type

    def name(self) -> str:
        return self._name

    def uri(self) -> str:
        return self._uri

    def type(self) -> int:
        return self._type


def _detail_uri(provider: str, source: str, name: str) -> str:
    """provider 自己给子图层的 URI：ogr 拼 ``layername``，gdal 就是那个路径。

    The URI a provider itself gives a sublayer: ogr joins ``layername``, gdal keeps the
    path (both measured).
    """

    return f"{source}|layername={name}" if provider == "ogr" else source


class FakeCrs:
    def __init__(self, authid: str) -> None:
        self._authid = authid

    def authid(self) -> str:
        return self._authid


class FakeRenderer:
    """有渲染器的那一点：读侧报的类型名。

    Enough of a renderer: the type name the read side reports.
    """

    def __init__(self, renderer_type: str) -> None:
        self._renderer_type = renderer_type

    def type(self) -> str:
        return self._renderer_type


class FakeSymbol:
    """一次 ``createSimple`` 的产物：只有声明的那些属性。

    What a ``createSimple`` call produced: the declared properties, nothing else.
    """

    def __init__(self, kind: str, properties: dict[str, Any]) -> None:
        self.kind = kind
        self.properties = dict(properties)


# 假件的 ``QVariant.Type`` 值。
# 真实值依次是 1、2、4、10、14。
# The fake's ``QVariant.Type`` values.
# The real values are 1, 2, 4, 10, 14.
VARIANT_STRING = 10


class FakeQVariant:
    """一个值，以及 setter 对它做的那一次转换。

    ``convert`` 照抄实测行为：日期目标收 ISO 日期串，别的都拒。
    布尔目标收任何字符串，只有 ``'false'``/``'0'``/``''`` 为假。
    字符串目标把任何值变成它的文本。

    A value and the one conversion the setter asks of it.  ``convert`` mirrors the
    measured behaviour: a date target takes an ISO date string and refuses everything
    else, a boolean target takes every string but ``'false'``/``'0'``/``''``, and a
    string target takes any value as its text.
    """

    Bool = 1
    Int = 2
    LongLong = 4
    String = VARIANT_STRING
    Date = 14

    def __init__(self, value: Any) -> None:
        self.converted: Any = value

    def convert(self, target_type: int) -> bool:
        value = self.converted
        if target_type == FakeQVariant.Date:
            try:
                self.converted = date.fromisoformat(value) if isinstance(value, str) else None
            except ValueError:
                self.converted = None
            return self.converted is not None
        if target_type == FakeQVariant.Bool:
            if isinstance(value, str):
                self.converted = value.strip().lower() not in {"", "0", "false"}
                return True
            if isinstance(value, (bool, int, float)):
                self.converted = bool(value)
                return True
        elif target_type == FakeQVariant.String:
            # 实测：bool 变 ``'true'``/``'false'``。
            # 整数浮点丢掉 ``.0``。
            # A bool becomes ``'true'``/``'false'``.
            # A whole float loses its ``.0``.
            if isinstance(value, bool):
                self.converted = "true" if value else "false"
            elif isinstance(value, float) and value.is_integer():
                self.converted = str(int(value))
            else:
                self.converted = value if isinstance(value, str) else str(value)
            return True
        self.converted = None
        return False

    def value(self) -> Any:
        return self.converted


class FakeField:
    """setter 从一个字段上读的东西：是不是数值，以及它的 QVariant 类型。

    What the setter reads of a field: its numeric kind and its QVariant type.
    """

    def __init__(self, name: str, numeric: bool = True, field_type: int = VARIANT_STRING) -> None:
        self.name = name
        self.numeric = numeric
        self.field_type = field_type

    def isNumeric(self) -> bool:
        return self.numeric

    def type(self) -> int:
        return self.field_type


class FakeFields:
    def __init__(self, fields: list[Any]) -> None:
        self.items = list(fields)

    def indexFromName(self, name: str) -> int:
        for index, field in enumerate(self.items):
            if field.name == name:
                return index
        return -1

    def at(self, index: int) -> Any:
        return self.items[index]


class FakeColor:
    """栅格 setter 眼里的 ``QColor``：它就是构造时用的那个字符串。

    ``QColor`` for the raster setter: the string it was built from.
    """

    def __init__(self, color: str) -> None:
        self._color = color

    def name(self) -> str:
        return self._color.lower()


class FakeRule:
    """一条 ``QgsRuleBasedRenderer.Rule``：自己的过滤、符号和子规则。

    One ``QgsRuleBasedRenderer.Rule``: its own filter, symbol and children.
    """

    def __init__(
        self,
        symbol: Any,
        filterExp: str = "",
        label: str = "",
        elseRule: bool = False,
    ) -> None:
        self.symbol = symbol
        self.filter = filterExp
        self.label = label
        self.else_rule = elseRule
        self.active: bool | None = None
        self.children: list[FakeRule] = []

    def appendChild(self, child: FakeRule) -> None:
        self.children.append(child)

    def setActive(self, active: bool) -> None:
        self.active = active


class FakeDocument:
    """``QDomDocument`` 的替身：里面装什么由假图层决定。

    Stand-in for ``QDomDocument``: the fake layer decides what it holds.
    """

    def __init__(self) -> None:
        self.content = ""

    def setContent(self, xml: str) -> None:
        self.content = xml

    def toString(self) -> str:
        return self.content


class FakeLayer:
    """``QgsMapLayer`` 够用的那部分：身份、provider、CRS、有效性、样式。

    Enough of ``QgsMapLayer``: identity, provider, CRS, validity, style.
    """

    def __init__(
        self,
        layer_id: str,
        name: str,
        provider: str = "ogr",
        authid: str = "EPSG:4326",
        valid: bool = True,
    ) -> None:
        self.layer_id = layer_id
        self.layer_name = name
        self.provider = provider
        self.authid = authid
        self.valid = valid
        # 样式读侧：几何类型、渲染器和一次原生导出。
        # 几何类型、渲染器、原生导出。
        # The style read side.
        # A geometry type, a renderer and a native export.
        self.geometry = 0
        self.renderer_value: FakeRenderer | None = None
        self.style_xml = "<qgis/>"
        self.style_error = ""
        self.style_exports: list[tuple[Any, Any]] = []
        # 写侧：装上了什么、图层里有什么、以及调用记录。
        # 装上了什么、图层里有什么、调用记录。
        # The setter side.
        # What was installed and what was called.
        self.renderer_sets: list[Any] = []
        self.repaints = 0
        self.events: list[str] = []
        self.band_count = 1
        self.data_provider: Any = None
        self.field_list: list[Any] = []
        # 栅格类型：默认普通栅格（GrayOrUndefined=0）。
        # The raster type: an ordinary raster by default (GrayOrUndefined = 0).
        self.raster_type = 0

    def id(self) -> str:
        return self.layer_id

    def name(self) -> str:
        return self.layer_name

    def providerType(self) -> str:
        return self.provider

    def crs(self) -> FakeCrs:
        return FakeCrs(self.authid)

    def isValid(self) -> bool:
        return self.valid

    def geometryType(self) -> int:
        return self.geometry

    def rasterType(self) -> int:
        return self.raster_type

    def renderer(self) -> FakeRenderer | None:
        return self.renderer_value

    def exportNamedStyle(self, document: FakeDocument, context: Any, categories: Any) -> str:
        self.style_exports.append((context, categories))
        document.setContent(self.style_xml)
        return self.style_error

    def bandCount(self) -> int:
        return self.band_count

    def dataProvider(self) -> Any:
        return self.data_provider

    def fields(self) -> FakeFields:
        return FakeFields(self.field_list)

    def setRenderer(self, renderer: Any) -> None:
        self.renderer_sets.append(renderer)
        self.renderer_value = renderer
        self.events.append("setRenderer")

    def triggerRepaint(self) -> None:
        self.repaints += 1
        self.events.append("repaint")


class FakeGroup:
    """父分组；取消勾选图层时它保持不变，打勾时跟着勾。

    A parent group; it stays as it is while a layer is unchecked, and is ticked along
    with one that is checked.
    """

    def __init__(self, checked: bool = True, parent: FakeGroup | None = None) -> None:
        self.checked = checked
        self.parent = parent
        self.sets: list[bool] = []

    def itemVisibilityChecked(self) -> bool:
        return self.checked

    def isVisible(self) -> bool:
        return self.checked and (self.parent is None or self.parent.isVisible())

    def setItemVisibilityChecked(self, visible: bool) -> None:
        # 原生对同值早返回（qgslayertreenode.cpp:98-99），所以不记录一次空写。
        # Native returns early on the same value, so a no-op write is not recorded.
        if self.checked == visible:
            return
        self.sets.append(visible)
        self.checked = visible

    def setItemVisibilityCheckedParentRecursive(self, visible: bool) -> None:
        self.setItemVisibilityChecked(visible)
        if self.parent is not None:
            self.parent.setItemVisibilityCheckedParentRecursive(visible)


class FakeNode:
    """图层树节点：自己的勾选状态，以及它上面的分组。

    A layer tree node: its own check state, and the group above it.
    """

    def __init__(self, parent: FakeGroup | None = None, checked: bool = True) -> None:
        self.checked = checked
        self.parent = parent
        self.sets: list[bool] = []

    def itemVisibilityChecked(self) -> bool:
        return self.checked

    def isVisible(self) -> bool:
        return self.checked and (self.parent is None or self.parent.isVisible())

    def setItemVisibilityChecked(self, visible: bool) -> None:
        if self.checked == visible:
            return
        self.sets.append(visible)
        self.checked = visible

    def setItemVisibilityCheckedParentRecursive(self, visible: bool) -> None:
        # 就是原生那个递归：自己，然后所有祖先。
        # The native recursion: this node, then every ancestor.
        self.setItemVisibilityChecked(visible)
        if self.parent is not None:
            self.parent.setItemVisibilityCheckedParentRecursive(visible)


class FakeTreeRoot:
    def __init__(self) -> None:
        self.nodes: dict[str, FakeNode] = {}

    def findLayer(self, layer_id: str) -> FakeNode | None:
        return self.nodes.get(layer_id)


class FakeProject:
    def __init__(self) -> None:
        self.layers: dict[str, Any] = {}
        self.root = FakeTreeRoot()
        self.added: list[tuple[Any, bool]] = []
        self.dirty: list[bool] = []

    def mapLayers(self) -> dict[str, Any]:
        return dict(self.layers)

    def mapLayer(self, layer_id: str) -> Any | None:
        return self.layers.get(layer_id)

    def layerTreeRoot(self) -> FakeTreeRoot:
        return self.root

    def setDirty(self, dirty: bool = True) -> None:
        self.dirty.append(dirty)

    def addMapLayer(self, layer: Any, add_to_legend: bool = True) -> Any:
        # 与 QGIS 一致：没有图例条目，图层就没有树节点。
        # No legend entry, no tree node.
        self.layers[layer.id()] = layer
        self.added.append((layer, add_to_legend))
        if add_to_legend:
            self.root.nodes[layer.id()] = FakeNode()
        return layer


class FakeQgis:
    """只装图层操作会 import 的那些东西的 ``qgis.core``。

    A ``qgis.core`` holding only what the layer operations import.
    """

    def __init__(self) -> None:
        self.constructed: list[Any] = []
        self.encoded: list[dict[str, str]] = []
        self.provider_names: list[str] = []
        self.valid = True
        # provider 的清单：``querySublayers`` 报什么，能不能查。
        # What the provider's ``querySublayers`` reports, and whether it can be asked.
        self.sublayer_names: list[str] = ["roads"]
        self.sublayer_queries: list[str] = []
        self.query_sublayers_capability = 4  # 对应 QgsProviderMetadata.QuerySublayers
        holder = self

        # 活的注册表：渲染器名字要从这里读，不能猜。
        # 渲染器名字从这里读，不猜。
        # The live registries.
        # The renderer names are read, never guessed.
        self.vector_renderer_types: list[str] = []
        self.raster_renderer_types: list[str] = []
        self.renderer_registry_layers: list[Any] = []
        self.raster_registries_built = 0

        # 渲染器 setter：造了什么、按什么顺序、什么可以失败。
        # 造了什么、什么顺序、什么会失败。
        # The renderer setter.
        # What was built, in what order, what fails.
        self.events: list[str] = []
        self.built: list[Any] = []
        self.invalid_expressions: set[str] = set()
        self.fail_build: str | None = None
        self.raster_data_type = 1  # 对应 Qgis.DataType.Byte / Qgis.DataType.Byte

        QgsMapLayer = type(
            "QgsMapLayer",
            (),
            {category: 1 << position for position, category in enumerate(VECTOR_CATEGORIES)}
            | {
                "AllStyleCategories": (1 << len(VECTOR_CATEGORIES)) - 1,
                # 原生那个“一个都不选”的位标志（实测 ``QgsMapLayer.StyleCategories()`` 为 0）。
                # The native "nothing selected" flag (measured: it is 0).
                "StyleCategories": staticmethod(lambda: 0),
            },
        )

        class QgsReadWriteContext:
            pass

        class VectorRendererRegistry:
            def renderersList(self, layer: Any) -> list[str]:
                holder.renderer_registry_layers.append(layer)
                return list(holder.vector_renderer_types)

        vector_registry = VectorRendererRegistry()

        class QgsApplication:
            @staticmethod
            def rendererRegistry() -> VectorRendererRegistry:
                return vector_registry

        class QgsRasterRendererRegistry:
            def __init__(self) -> None:
                holder.raster_registries_built += 1

            def renderersList(self) -> list[str]:
                return list(holder.raster_renderer_types)

        Qgis = types.SimpleNamespace(
            GeometryType=types.SimpleNamespace(Point=0, Line=1, Polygon=2, Unknown=3, Null=4),
            DataType=types.SimpleNamespace(Byte=1, Float32=6),
            LayerType=types.SimpleNamespace(Vector=0, Raster=1),
            RasterLayerType=types.SimpleNamespace(
                GrayOrUndefined=0, Palette=1, MultiBand=2, SingleBandColorData=3
            ),
        )
        QVariant = FakeQVariant

        class FakeRasterProvider:
            def __init__(self) -> None:
                self.data_type_bands: list[int] = []

            def dataType(self, band: int) -> int:
                self.data_type_bands.append(band)
                return holder.raster_data_type

        class QgsVectorLayer(FakeLayer):
            def __init__(self, uri: str, name: str, provider: str, layer_id: str | None = None):
                super().__init__(
                    layer_id or f"vector-{len(holder.constructed)}",
                    name,
                    provider,
                    valid=holder.valid,
                )
                self.source_uri = uri
                holder.constructed.append(self)

        class QgsRasterLayer(FakeLayer):
            def __init__(self, uri: str, name: str, provider: str, layer_id: str | None = None):
                super().__init__(
                    layer_id or f"raster-{len(holder.constructed)}",
                    name,
                    provider,
                    authid="EPSG:3857",
                    valid=holder.valid,
                )
                self.source_uri = uri
                self.data_provider = FakeRasterProvider()
                holder.constructed.append(self)

        # --- 渲染器 setter / the renderer setter -----------------------------

        class FakeBuiltRenderer:
            renderer_type = ""

            def type(self) -> str:
                return self.renderer_type

        def record(kind: str, rendered: Any) -> None:
            holder.built.append(rendered)
            holder.events.append(f"construct:{kind}")
            if holder.fail_build == kind:
                raise RuntimeError("the native constructor refused")

        class QgsMarkerSymbol:
            @staticmethod
            def createSimple(properties: dict[str, Any]) -> FakeSymbol:
                symbol = FakeSymbol("marker", properties)
                record("markerSymbol", symbol)
                return symbol

        class QgsLineSymbol:
            @staticmethod
            def createSimple(properties: dict[str, Any]) -> FakeSymbol:
                symbol = FakeSymbol("line", properties)
                record("lineSymbol", symbol)
                return symbol

        class QgsFillSymbol:
            @staticmethod
            def createSimple(properties: dict[str, Any]) -> FakeSymbol:
                symbol = FakeSymbol("fill", properties)
                record("fillSymbol", symbol)
                return symbol

        class QgsNullSymbolRenderer(FakeBuiltRenderer):
            renderer_type = "nullSymbol"

            def __init__(self) -> None:
                record("nullSymbol", self)

        class QgsSingleSymbolRenderer(FakeBuiltRenderer):
            renderer_type = "singleSymbol"

            def __init__(self, symbol: FakeSymbol) -> None:
                self.symbol = symbol
                record("singleSymbol", self)

        class QgsRendererCategory:
            def __init__(self, value: Any, symbol: FakeSymbol, label: str) -> None:
                self.value = value
                self.symbol = symbol
                self.label = label
                record("category", self)

        class QgsCategorizedSymbolRenderer(FakeBuiltRenderer):
            renderer_type = "categorizedSymbol"

            def __init__(self, field: str, categories: list[QgsRendererCategory]) -> None:
                self.field = field
                self.categories = list(categories)
                record("categorizedSymbol", self)

            def categoryIndexForValue(self, value: Any) -> int:
                # 实测：原生查找按数值比较。
                # 所以 int 类别能匹配 float 查的值。
                # Measured: the native lookup compares numerically.
                # So an int category matches a float value.
                for index, category in enumerate(self.categories):
                    if category.value == value:
                        return index
                return -1

        class QgsRendererRange:
            def __init__(self, lower: float, upper: float, symbol: FakeSymbol, label: str) -> None:
                self.lower = lower
                self.upper = upper
                self.symbol = symbol
                self.label = label

        class QgsGraduatedSymbolRenderer(FakeBuiltRenderer):
            renderer_type = "graduatedSymbol"

            def __init__(self, field: str, ranges: list[QgsRendererRange]) -> None:
                self.field = field
                self.ranges = list(ranges)
                record("graduatedSymbol", self)

        class QgsRuleBasedRenderer(FakeBuiltRenderer):
            renderer_type = "RuleRenderer"
            Rule = FakeRule

            def __init__(self, root: FakeRule) -> None:
                self.root = root
                record("RuleRenderer", self)

        class QgsExpression:
            def __init__(self, text: str) -> None:
                self.text = text

            def hasParserError(self) -> bool:
                return self.text in holder.invalid_expressions

        class QgsContrastEnhancement:
            StretchToMinimumMaximum = 1

            def __init__(self, data_type: int) -> None:
                self.data_type = data_type
                self.minimum: float | None = None
                self.maximum: float | None = None
                self.algorithm: int | None = None
                self.minimum_table: bool | None = None
                self.maximum_table: bool | None = None

            def setMinimumValue(self, value: float, generateTable: bool = True) -> None:
                self.minimum = value
                self.minimum_table = generateTable
                holder.events.append("minimum")

            def setMaximumValue(self, value: float, generateTable: bool = True) -> None:
                self.maximum = value
                self.maximum_table = generateTable
                holder.events.append("maximum")

            def setContrastEnhancementAlgorithm(self, algorithm: int) -> None:
                self.algorithm = algorithm
                holder.events.append("algorithm")

        class QgsSingleBandGrayRenderer(FakeBuiltRenderer):
            renderer_type = "singlebandgray"

            def __init__(self, provider: Any, band: int) -> None:
                self.provider = provider
                self.band = band
                self.enhancement: QgsContrastEnhancement | None = None
                record("singlebandgray", self)

            def setContrastEnhancement(self, enhancement: QgsContrastEnhancement) -> None:
                self.enhancement = enhancement
                holder.events.append("contrast")

        class QgsMultiBandColorRenderer(FakeBuiltRenderer):
            renderer_type = "multibandcolor"

            def __init__(self, provider: Any, red: int, green: int, blue: int) -> None:
                self.provider = provider
                self.red = red
                self.green = green
                self.blue = blue
                record("multibandcolor", self)

        class QgsColorRampShader:
            Interpolated = 0
            Discrete = 1
            Exact = 2

            class ColorRampItem:
                def __init__(self, value: float, color: FakeColor, label: str = "") -> None:
                    self.value = value
                    self.color = color
                    self.label = label

            def __init__(self) -> None:
                self.ramp_type: int | None = None
                self.items: list[QgsColorRampShader.ColorRampItem] = []

            def setColorRampType(self, ramp_type: int) -> None:
                self.ramp_type = ramp_type

            def setColorRampItemList(self, items: list[QgsColorRampShader.ColorRampItem]) -> None:
                self.items = list(items)

        class QgsRasterShader:
            def __init__(self) -> None:
                self.function: Any = None

            def setRasterShaderFunction(self, function: Any) -> None:
                self.function = function

        class QgsSingleBandPseudoColorRenderer(FakeBuiltRenderer):
            renderer_type = "singlebandpseudocolor"

            def __init__(self, provider: Any, band: int, shader: QgsRasterShader) -> None:
                self.provider = provider
                self.band = band
                self.shader = shader
                record("singlebandpseudocolor", self)

        class QgsRasterSingleColorRenderer(FakeBuiltRenderer):
            renderer_type = "singlecolor"

            def __init__(self, provider: Any, band: int, color: FakeColor) -> None:
                self.provider = provider
                self.band = band
                self.color = color
                record("singlecolor", self)

        class ProviderMetadata:
            """一个 provider 的元数据：会按自己的 key 报对应类型的子图层。

            One provider's metadata: it reports sublayers of its own layer type.
            """

            def __init__(self, key: str) -> None:
                self.key = key

            def encodeUri(self, parts: dict[str, str]) -> str:
                holder.encoded.append(dict(parts))
                return f"{parts['path']}|layername={parts['layerName']}"

            def capabilities(self) -> int:
                return holder.query_sublayers_capability

            def querySublayers(self, uri: str) -> list[Any]:
                holder.sublayer_queries.append(uri)
                layer_type = 0 if self.key == "ogr" else 1
                return [
                    FakeSublayerDetails(name, _detail_uri(self.key, uri, name), layer_type)
                    for name in holder.sublayer_names
                ]

        class QgsProviderMetadata:
            QuerySublayers = 4

        class QgsProviderRegistry:
            @staticmethod
            def instance() -> FakeQgis:
                return holder

        self.QgsVectorLayer = QgsVectorLayer
        self.QgsRasterLayer = QgsRasterLayer
        self.ProviderMetadata = ProviderMetadata
        self.QgsProviderRegistry = QgsProviderRegistry
        self.QgsProviderMetadata = QgsProviderMetadata
        self.QgsMapLayer = QgsMapLayer
        self.QgsReadWriteContext = QgsReadWriteContext
        self.QgsApplication = QgsApplication
        self.QgsRasterRendererRegistry = QgsRasterRendererRegistry
        self.Qgis = Qgis
        self.QVariant: Any = QVariant
        # setter 用到的原生类。
        # 供测试里的 isinstance 断言用。
        # The setter's native classes.
        # For the tests' isinstance assertions.
        self.QgsMarkerSymbol: Any = QgsMarkerSymbol
        self.QgsLineSymbol: Any = QgsLineSymbol
        self.QgsFillSymbol: Any = QgsFillSymbol
        self.QgsNullSymbolRenderer: Any = QgsNullSymbolRenderer
        self.QgsSingleSymbolRenderer: Any = QgsSingleSymbolRenderer
        self.QgsRendererCategory: Any = QgsRendererCategory
        self.QgsCategorizedSymbolRenderer: Any = QgsCategorizedSymbolRenderer
        self.QgsRendererRange: Any = QgsRendererRange
        self.QgsGraduatedSymbolRenderer: Any = QgsGraduatedSymbolRenderer
        self.QgsRuleBasedRenderer: Any = QgsRuleBasedRenderer
        self.QgsExpression: Any = QgsExpression
        self.QgsContrastEnhancement: Any = QgsContrastEnhancement
        self.QgsSingleBandGrayRenderer: Any = QgsSingleBandGrayRenderer
        self.QgsMultiBandColorRenderer: Any = QgsMultiBandColorRenderer
        self.QgsColorRampShader: Any = QgsColorRampShader
        self.QgsRasterShader: Any = QgsRasterShader
        self.QgsSingleBandPseudoColorRenderer: Any = QgsSingleBandPseudoColorRenderer
        self.QgsRasterSingleColorRenderer: Any = QgsRasterSingleColorRenderer

    def providerMetadata(self, name: str) -> Any:
        self.provider_names.append(name)
        return self.ProviderMetadata(name)


@pytest.fixture
def fake_qgis(monkeypatch: pytest.MonkeyPatch) -> FakeQgis:
    fake = FakeQgis()
    core = fake_module(
        "qgis.core",
        QgsVectorLayer=fake.QgsVectorLayer,
        QgsRasterLayer=fake.QgsRasterLayer,
        QgsProviderRegistry=fake.QgsProviderRegistry,
        QgsProviderMetadata=fake.QgsProviderMetadata,
        QgsMapLayer=fake.QgsMapLayer,
        QgsReadWriteContext=fake.QgsReadWriteContext,
        QgsApplication=fake.QgsApplication,
        QgsRasterRendererRegistry=fake.QgsRasterRendererRegistry,
        Qgis=fake.Qgis,
        QgsMarkerSymbol=fake.QgsMarkerSymbol,
        QgsLineSymbol=fake.QgsLineSymbol,
        QgsFillSymbol=fake.QgsFillSymbol,
        QgsNullSymbolRenderer=fake.QgsNullSymbolRenderer,
        QgsSingleSymbolRenderer=fake.QgsSingleSymbolRenderer,
        QgsRendererCategory=fake.QgsRendererCategory,
        QgsCategorizedSymbolRenderer=fake.QgsCategorizedSymbolRenderer,
        QgsRendererRange=fake.QgsRendererRange,
        QgsGraduatedSymbolRenderer=fake.QgsGraduatedSymbolRenderer,
        QgsRuleBasedRenderer=fake.QgsRuleBasedRenderer,
        QgsExpression=fake.QgsExpression,
        QgsContrastEnhancement=fake.QgsContrastEnhancement,
        QgsSingleBandGrayRenderer=fake.QgsSingleBandGrayRenderer,
        QgsMultiBandColorRenderer=fake.QgsMultiBandColorRenderer,
        QgsColorRampShader=fake.QgsColorRampShader,
        QgsRasterShader=fake.QgsRasterShader,
        QgsSingleBandPseudoColorRenderer=fake.QgsSingleBandPseudoColorRenderer,
        QgsRasterSingleColorRenderer=fake.QgsRasterSingleColorRenderer,
    )
    qtxml = fake_module("qgis.PyQt.QtXml", QDomDocument=FakeDocument)
    qtgui = fake_module("qgis.PyQt.QtGui", QColor=FakeColor)
    qtcore = fake_module("qgis.PyQt.QtCore", QVariant=fake.QVariant)
    pyqt = fake_module("qgis.PyQt", QtXml=qtxml, QtGui=qtgui, QtCore=qtcore)
    qgis_module = fake_module("qgis", core=core, PyQt=pyqt)
    monkeypatch.setitem(sys.modules, "qgis", qgis_module)
    monkeypatch.setitem(sys.modules, "qgis.core", core)
    monkeypatch.setitem(sys.modules, "qgis.PyQt", pyqt)
    monkeypatch.setitem(sys.modules, "qgis.PyQt.QtXml", qtxml)
    monkeypatch.setitem(sys.modules, "qgis.PyQt.QtGui", qtgui)
    monkeypatch.setitem(sys.modules, "qgis.PyQt.QtCore", qtcore)
    return fake


# --- 辅助函数 / helpers ------------------------------------------------------


def fake_module(name: str, **attributes: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    vars(module).update(attributes)
    return module


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    # 根目录里带个空格。
    # 真实的 ogr URI 必须能扈住它。
    # A space in the root.
    # The real ogr URI has to survive one.
    root = tmp_path / "data root"
    root.mkdir()
    return root


def make_source(root: Path, name: str = "roads.gpkg") -> Path:
    path = root / name
    path.write_text("x", encoding="utf-8")
    return path


def make_context(project: Any) -> OperationContext:
    return OperationContext(iface=None, project=project, lifecycle=None, policy=Settings())


def make_layer(
    fake: FakeQgis,
    layer_id: str,
    name: str = "layer",
    kind: str = "vector",
    provider: str = "ogr",
) -> Any:
    layer_class = fake.QgsVectorLayer if kind == "vector" else fake.QgsRasterLayer
    layer = layer_class(f"source-{layer_id}", name, provider, layer_id=layer_id)
    # setter 测试断言这三步的顺序。
    # 构造、安装、重绘。
    # The setter tests assert the order.
    # Construction, install and repaint.
    layer.events = fake.events
    return layer


def add_params(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "kind": "vector",
        "source_path": "C:/data/roads.gpkg",
        "name": "Roads",
    }
    values.update(overrides)
    return values


def refuse(operation: Any, ctx: OperationContext, request: Any) -> str:
    with pytest.raises(FrameworkError) as caught:
        operation.execute_main(ctx, request)
    return caught.value.code


# --- 输入模型 / input models -----------------------------------------------


def test_an_undeclared_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        ops.LayerAddParams.model_validate(add_params(provider="postgres"))


@pytest.mark.parametrize("kind", ["other", "VECTOR", "points"])
def test_the_kind_is_a_two_valued_enumeration(kind: str) -> None:
    with pytest.raises(ValidationError):
        ops.LayerAddParams.model_validate(add_params(kind=kind))


def test_an_empty_source_path_is_refused() -> None:
    with pytest.raises(ValidationError):
        ops.LayerAddParams.model_validate(add_params(source_path=""))


def test_an_empty_name_is_refused() -> None:
    with pytest.raises(ValidationError):
        ops.LayerAddParams.model_validate(add_params(name=""))


@pytest.mark.parametrize("length", [128, 200, 4096])
def test_a_layer_name_of_any_length_is_accepted(length: int) -> None:
    # 桌面版对图层名没有长度上限（实测：200 字的名字原样保住），所以插件也不设。
    # Desktop puts no length limit on a layer name (measured: a 200-character name is kept
    # as is), so neither does the plugin.
    params = ops.LayerAddParams.model_validate(add_params(name="x" * length))
    assert len(params.name) == length


@pytest.mark.parametrize(
    "sub_layer", ["roads", "roads_1-A", "路段 2024", "roads/1", "roads 1", "x" * 200]
)
def test_any_sub_layer_name_the_provider_can_report_is_accepted(sub_layer: str) -> None:
    # 子图层的名字是 provider 的自由文本，转义由它的 encodeUri 负责。
    # A sub-layer name is the provider's free text, and its encodeUri owns the escaping.
    assert ops.LayerAddParams.model_validate(add_params(sub_layer=sub_layer)).sub_layer == sub_layer


def test_an_empty_sub_layer_is_refused() -> None:
    with pytest.raises(ValidationError):
        ops.LayerAddParams.model_validate(add_params(sub_layer=""))


def test_a_raster_may_carry_a_sub_layer() -> None:
    params = ops.LayerAddParams.model_validate(add_params(kind="raster", sub_layer="dem"))
    assert params.sub_layer == "dem"


def test_a_layer_id_cannot_be_empty() -> None:
    with pytest.raises(ValidationError):
        ops.LayerGetInfoParams.model_validate({"layer_id": ""})
    with pytest.raises(ValidationError):
        ops.LayerSetVisibilityParams.model_validate({"layer_id": "", "visible": True})


def test_a_visibility_flag_must_be_a_real_boolean() -> None:
    with pytest.raises(ValidationError):
        ops.LayerSetVisibilityParams.model_validate({"layer_id": "x", "visible": "false"})


# --- layer.add：加图层 / adding a layer ----------------------------------


def test_a_source_the_provider_does_not_recognise_is_invalid_layer(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """provider 的清单为空才是拒的理由（桌面版那句 "Invalid Data Source"）。

    An empty provider list is the refusal (desktop's "Invalid Data Source").
    """

    fake_qgis.sublayer_names = []
    project = FakeProject()
    code = refuse(
        ops.LayerAdd(),
        make_context(project),
        ops.LayerAddParams(**add_params(source_path=str(make_source(data_root)))),
    )
    assert code == INVALID_LAYER
    assert project.added == []
    assert project.mapLayers() == {}
    assert fake_qgis.sublayer_queries == [str(make_source(data_root).resolve())]


def test_a_recognised_source_qgis_cannot_read_is_still_added(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """provider 认得、图层读不出：桌面版照样加进图例，靠坏图层图标提示。

    Recognised by the provider but unreadable: desktop still adds it and leaves the
    bad-layer icon to say so.
    """

    fake_qgis.valid = False
    project = FakeProject()
    ctx = make_context(project)
    result = ops.LayerAdd().execute_main(
        ctx,
        ops.LayerAddParams(**add_params(source_path=str(make_source(data_root)))),
    )

    info = ops.LayerGetInfo().execute_main(ctx, ops.LayerGetInfoParams(layer_id=result.layer_id))
    assert info.valid is False
    assert project.mapLayer(result.layer_id) is fake_qgis.constructed[0]
    assert result.crs_authid == "EPSG:4326"


def test_a_sub_layer_the_provider_did_not_report_is_invalid_layer(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """桌面版的选择框只列得出来的东西，所以点不出一个不存在的名字。

    Desktop's dialog only lists what exists, so a name it never offered is not an answer.
    """

    fake_qgis.sublayer_names = ["roads"]
    code = refuse(
        ops.LayerAdd(),
        make_context(FakeProject()),
        ops.LayerAddParams(
            **add_params(source_path=str(make_source(data_root)), sub_layer="parcels")
        ),
    )
    assert code == INVALID_LAYER


def test_a_vector_source_is_added_through_ogr_without_a_sub_layer(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    source = make_source(data_root)
    result = ops.LayerAdd().execute_main(
        make_context(project),
        ops.LayerAddParams(**add_params(source_path=str(source))),
    )

    built = fake_qgis.constructed[0]
    assert result.layer_id == built.id()
    assert result.name == "Roads"
    assert result.kind == "vector"
    assert result.provider == "ogr"
    assert result.crs_authid == "EPSG:4326"
    assert built.source_uri == str(source.resolve())
    assert built.provider == "ogr"
    assert fake_qgis.encoded == []
    assert project.layers[result.layer_id] is built
    assert project.added == [(built, True)]


def test_a_sub_layer_uri_comes_from_the_providers_own_detail(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    source = make_source(data_root, "roads with space.gpkg")
    result = ops.LayerAdd().execute_main(
        make_context(project),
        ops.LayerAddParams(**add_params(source_path=str(source), sub_layer="roads")),
    )

    resolved = str(source.resolve())
    # URI 就是那条 detail 的 uri()——桌面版 toLayer() 交给 QgsMapLayerFactory 的同一个字符串
    # （qgsprovidersublayerdetails.cpp:22-28），不再自己调 encodeUri 拼。
    # The URI is that detail's own uri() -- the same string desktop's toLayer() hands to
    # QgsMapLayerFactory (qgsprovidersublayerdetails.cpp:22-28); nothing re-encodes it here.
    assert fake_qgis.provider_names == ["ogr"]
    assert fake_qgis.encoded == []
    assert fake_qgis.constructed[0].source_uri == f"{resolved}|layername=roads"
    assert result.provider == "ogr"


def test_a_raster_can_carry_a_sub_layer(fake_qgis: FakeQgis, data_root: Path) -> None:
    """栅格也有子数据集清单：桌面版那个页签同样提供它（`qgsapplayerhandling.cpp:1363-1381`）。

    A raster has a subdataset list too: desktop's raster tab offers one as well
    (`qgsapplayerhandling.cpp:1363-1381`).
    """

    fake_qgis.sublayer_names = ["dem"]
    project = FakeProject()
    source = make_source(data_root, "dem.tif")
    result = ops.LayerAdd().execute_main(
        make_context(project),
        ops.LayerAddParams(**add_params(kind="raster", source_path=str(source), sub_layer="dem")),
    )

    built = fake_qgis.constructed[0]
    # gdal 自己给的子数据集 URI 就是那个路径（实测），不是 `path|name`。
    # The URI gdal gives a subdataset is the path itself (measured), not `path|name`.
    assert built.source_uri == str(source.resolve())
    assert built.provider == "gdal"
    assert result.provider == "gdal"
    assert fake_qgis.encoded == []


def test_a_raster_source_is_added_through_gdal(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    source = make_source(data_root, "dem.tif")
    result = ops.LayerAdd().execute_main(
        make_context(project),
        ops.LayerAddParams(**add_params(kind="raster", source_path=str(source))),
    )

    built = fake_qgis.constructed[0]
    assert result.kind == "raster"
    assert result.provider == "gdal"
    assert built.source_uri == str(source.resolve())
    assert built.provider == "gdal"
    assert fake_qgis.encoded == []


def test_a_legend_entry_is_always_created(fake_qgis: FakeQgis, data_root: Path) -> None:
    """没有 `add_to_legend` 参数：每次加图层都进图例（桌面版的 addToLegend=false 是“先进工程、
    随后进分组”，图层仍在图例里，`qgsapplayerhandling.cpp:802-808`）。

    No `add_to_legend` parameter: every add goes into the legend (desktop's
    addToLegend=false is "into the project, then into a group", and the layer is still in
    the legend, `qgsapplayerhandling.cpp:802-808`).
    """

    project = FakeProject()
    ctx = make_context(project)
    added = ops.LayerAdd().execute_main(
        ctx, ops.LayerAddParams(**add_params(source_path=str(make_source(data_root))))
    )
    info = ops.LayerGetInfo().execute_main(ctx, ops.LayerGetInfoParams(layer_id=added.layer_id))

    assert info.in_layer_tree is True
    assert (info.visible, info.effective_visible) == (True, True)
    assert "add_to_legend" not in ops.LayerAddParams.model_fields


def test_a_loaded_project_can_hold_a_layer_with_no_tree_node(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """加载来的工程里可以有不图层里的图层；那时 set_visibility 无从下手，但两个读照报。

    A loaded project can hold a layer that is not in the legend; set_visibility has nothing
    to act on then, while both reads still answer.
    """

    project = FakeProject()
    ctx = make_context(project)
    layer = make_layer(fake_qgis, "orphan")
    # 只进工程、不进图层树：这是工程文件能表达的形状，layer.add 造不出来它了。
    # In the project only, not in the tree: a shape a project file can express, and one
    # layer.add can no longer produce.
    project.layers[layer.id()] = layer

    info = ops.LayerGetInfo().execute_main(ctx, ops.LayerGetInfoParams(layer_id=layer.id()))
    assert (info.in_layer_tree, info.visible, info.effective_visible) == (False, None, None)
    listed = ops.LayerList().execute_main(ctx, ops.LayerListParams()).layers[0]
    assert (listed.in_layer_tree, listed.visible, listed.effective_visible) == (False, None, None)
    code = refuse(
        ops.LayerSetVisibility(),
        ctx,
        ops.LayerSetVisibilityParams(layer_id=layer.id(), visible=False),
    )
    assert code == LAYER_NOT_IN_TREE


# --- layer.list：列图层 / listing the layers ------------------------------


def test_layers_are_listed_in_id_order(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    for layer_id in ("zzz", "aaa", "mmm"):
        project.layers[layer_id] = make_layer(fake_qgis, layer_id)

    result = ops.LayerList().execute_main(make_context(project), ops.LayerListParams())
    assert [summary.layer_id for summary in result.layers] == ["aaa", "mmm", "zzz"]


def test_a_layer_without_a_tree_node_has_no_visibility(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = make_layer(fake_qgis, "untreed")
    project.layers[layer.id()] = layer

    result = ops.LayerList().execute_main(make_context(project), ops.LayerListParams())
    summary = result.layers[0]
    assert summary.in_layer_tree is False
    assert summary.visible is None
    assert summary.effective_visible is None
    assert summary.model_dump(mode="json")["visible"] is None


def test_a_hidden_parent_group_keeps_own_and_effective_visibility_apart(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = make_layer(fake_qgis, "grouped")
    project.layers[layer.id()] = layer
    project.root.nodes[layer.id()] = FakeNode(parent=FakeGroup(checked=False))

    result = ops.LayerList().execute_main(make_context(project), ops.LayerListParams())
    summary = result.layers[0]
    assert summary.in_layer_tree is True
    assert summary.visible is True
    assert summary.effective_visible is False


def test_a_layer_that_is_neither_vector_nor_raster_is_other(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    project.layers["plugin-1"] = FakeLayer("plugin-1", "annotations", provider="custom", authid="")
    project.root.nodes["plugin-1"] = FakeNode()

    result = ops.LayerList().execute_main(make_context(project), ops.LayerListParams())
    summary = result.layers[0]
    assert summary.kind == "other"
    assert summary.crs_authid == ""
    assert summary.visible is True


def test_list_and_get_info_take_every_field_from_its_layer(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """一个 vector 一个 raster，每个值都必须来自它自己那一层。

    A vector and a raster, so each value has to come from the layer it belongs to.
    """

    project = FakeProject()
    ctx = make_context(project)
    # 假件给 vector 4326、raster 3857。
    # 所以 CRS 换个位或写死都过不了。
    # A vector is 4326, a raster 3857.
    # So a swapped or constant CRS cannot pass.
    roads = make_layer(fake_qgis, "roads", name="Roads")
    dem = make_layer(fake_qgis, "dem", name="Dem", kind="raster", provider="gdal")
    for layer in (roads, dem):
        project.layers[layer.id()] = layer
        project.root.nodes[layer.id()] = FakeNode()

    summaries = {
        summary.layer_id: summary
        for summary in ops.LayerList().execute_main(ctx, ops.LayerListParams()).layers
    }
    assert sorted(summaries) == ["dem", "roads"]
    assert (summaries["roads"].name, summaries["roads"].kind, summaries["roads"].crs_authid) == (
        "Roads",
        "vector",
        "EPSG:4326",
    )
    assert (summaries["dem"].name, summaries["dem"].kind, summaries["dem"].crs_authid) == (
        "Dem",
        "raster",
        "EPSG:3857",
    )

    info = ops.LayerGetInfo().execute_main(ctx, ops.LayerGetInfoParams(layer_id="dem"))
    assert info.layer_id == "dem"
    assert (info.name, info.kind, info.crs_authid) == ("Dem", "raster", "EPSG:3857")
    assert info.provider == "gdal"
    assert info.valid is True


# --- layer.get_info：看图层 / reading one layer ---------------------------


def test_get_info_refuses_an_unknown_layer(fake_qgis: FakeQgis, data_root: Path) -> None:
    code = refuse(
        ops.LayerGetInfo(),
        make_context(FakeProject()),
        ops.LayerGetInfoParams(layer_id="missing"),
    )
    assert code == LAYER_NOT_FOUND


def test_get_info_reports_validity_and_the_same_visibility_as_list(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    ctx = make_context(project)
    layer = make_layer(fake_qgis, "roads")
    layer.valid = False
    project.layers[layer.id()] = layer
    project.root.nodes[layer.id()] = FakeNode(parent=FakeGroup(checked=False))

    info = ops.LayerGetInfo().execute_main(ctx, ops.LayerGetInfoParams(layer_id=layer.id()))
    listed = ops.LayerList().execute_main(ctx, ops.LayerListParams()).layers[0]

    assert info.provider == "ogr"
    assert info.valid is False
    assert (info.in_layer_tree, info.visible, info.effective_visible) == (
        listed.in_layer_tree,
        listed.visible,
        listed.effective_visible,
    )
    assert (info.visible, info.effective_visible) == (True, False)


# --- layer.set_visibility：设可见性 / setting visibility ------------------


def test_set_visibility_refuses_an_unknown_layer(fake_qgis: FakeQgis, data_root: Path) -> None:
    code = refuse(
        ops.LayerSetVisibility(),
        make_context(FakeProject()),
        ops.LayerSetVisibilityParams(layer_id="missing", visible=True),
    )
    assert code == LAYER_NOT_FOUND


def test_set_visibility_refuses_a_layer_without_a_tree_node(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = make_layer(fake_qgis, "untreed")
    project.layers[layer.id()] = layer
    code = refuse(
        ops.LayerSetVisibility(),
        make_context(project),
        ops.LayerSetVisibilityParams(layer_id=layer.id(), visible=True),
    )
    assert code == LAYER_NOT_IN_TREE


def test_setting_the_same_state_changes_nothing(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    layer = make_layer(fake_qgis, "roads")
    project.layers[layer.id()] = layer
    node = FakeNode()
    project.root.nodes[layer.id()] = node

    result = ops.LayerSetVisibility().execute_main(
        make_context(project),
        ops.LayerSetVisibilityParams(layer_id=layer.id(), visible=True),
    )
    assert result.changed is False
    assert node.sets == []
    assert (result.visible, result.effective_visible) == (True, True)


def test_unchecking_touches_only_that_node(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    layer = make_layer(fake_qgis, "roads")
    project.layers[layer.id()] = layer
    group = FakeGroup(checked=True)
    node = FakeNode(parent=group)
    project.root.nodes[layer.id()] = node

    result = ops.LayerSetVisibility().execute_main(
        make_context(project),
        ops.LayerSetVisibilityParams(layer_id=layer.id(), visible=False),
    )
    assert result.changed is True
    assert node.sets == [False]
    assert group.sets == []
    assert (result.visible, result.effective_visible) == (False, False)


def test_checking_a_layer_inside_a_hidden_group_checks_the_group(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """图例的勾选框对一个叶子节点打勾时连祖先一起勾，否则勾了也看不见。

    The legend checkbox ticks a leaf's ancestors along with it, or the tick shows nothing.
    """

    project = FakeProject()
    layer = make_layer(fake_qgis, "grouped")
    project.layers[layer.id()] = layer
    outer = FakeGroup(checked=False)
    group = FakeGroup(checked=False, parent=outer)
    node = FakeNode(parent=group)
    project.root.nodes[layer.id()] = node

    result = ops.LayerSetVisibility().execute_main(
        make_context(project),
        ops.LayerSetVisibilityParams(layer_id=layer.id(), visible=True),
    )
    assert result.changed is True
    # 节点本来就勾着，所以它自己那次写是空写（原生同值早返回）；真正变的是两个分组。
    # The node was already ticked, so its own write is a no-op (native early return);
    # what changes are the two groups.
    assert node.sets == []
    assert group.sets == [True]
    assert outer.sets == [True]
    assert (result.visible, result.effective_visible) == (True, True)


def test_an_unchecked_group_alone_makes_the_check_a_change(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """节点已经勾着时，可见性仍未达成，所以这一次仍是改变。

    With the node already ticked, the layer is still not visible, so this call is a change.
    """

    project = FakeProject()
    layer = make_layer(fake_qgis, "grouped")
    project.layers[layer.id()] = layer
    group = FakeGroup(checked=False)
    node = FakeNode(parent=group)
    project.root.nodes[layer.id()] = node

    result = ops.LayerSetVisibility().execute_main(
        make_context(project),
        ops.LayerSetVisibilityParams(layer_id=layer.id(), visible=True),
    )
    assert (result.changed, result.visible, result.effective_visible) == (True, True, True)
    assert node.sets == []
    assert group.sets == [True]


# --- 声明与导入规则 / the declarations and the import rule ----------------


def test_the_four_operations_register_as_main_thread_layer_capabilities() -> None:
    operations = (ops.LayerAdd(), ops.LayerList(), ops.LayerGetInfo(), ops.LayerSetVisibility())
    registry = OperationRegistry()
    for operation in operations:
        registry.register(operation)

    assert [
        (spec.name, spec.kind, spec.strategy, spec.destructive) for spec in registry.list_specs()
    ] == [
        ("layer.add", "command", "main_thread", False),
        ("layer.get_info", "query", "main_thread", False),
        ("layer.list", "query", "main_thread", False),
        ("layer.set_visibility", "command", "main_thread", False),
    ]


_GUARD = """
import sys

from qgis_agent_plugin.operations import layer

assert callable(layer.layer_source_uri)
leaked = sorted(
    name
    for name in sys.modules
    if name.split(".")[0] in {"qgis", "PyQt5", "PyQt6", "PySide2", "PySide6"}
)
assert not leaked, leaked
"""


def test_the_layer_module_imports_without_qgis() -> None:
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ}
    env["PYTHONPATH"] = os.pathsep.join([str(root / "src"), env.get("PYTHONPATH", "")])
    result = subprocess.run(
        [sys.executable, "-c", _GUARD], capture_output=True, text=True, env=env, cwd=root
    )
    assert result.returncode == 0, result.stderr


# --- 样式读操作 / the style reads -----------------------------------------

STYLE_READS = (
    ops.LayerListStyleCategories,
    ops.LayerGetQml,
    ops.LayerListAvailableRenderers,
    ops.LayerGetRenderer,
)


def style_layer(
    fake: FakeQgis,
    project: FakeProject,
    layer_id: str,
    kind: str = "vector",
    name: str = "layer",
) -> Any:
    layer = make_layer(fake, layer_id, name=name, kind=kind)
    # 一个空间图层总是带着渲染器；没有几何的图层没有（实测）。
    # A spatial layer always carries a renderer; a geometry-less one does not (measured).
    layer.renderer_value = FakeRenderer("singleSymbol")
    project.layers[layer.id()] = layer
    return layer


def style_request(operation: Any, layer_id: str, **overrides: Any) -> Any:
    return operation.input_model.model_validate({"layer_id": layer_id, **overrides})


def category_flag(fake: FakeQgis, names: Any) -> int:
    flag = 0
    for name in names:
        flag |= getattr(fake.QgsMapLayer, name)
    return flag


def test_a_spatial_vector_layer_has_the_eighteen_categories_in_order(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    style_layer(fake_qgis, project, "roads")

    result = ops.LayerListStyleCategories().execute_main(
        make_context(project),
        ops.LayerListStyleCategoriesParams(layer_id="roads"),
    )
    assert result.layer_id == "roads"
    assert tuple(result.categories) == VECTOR_CATEGORIES
    assert set(result.model_dump()) == {"layer_id", "categories"}


def test_a_raster_layer_has_the_nine_categories_in_order(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    style_layer(fake_qgis, project, "dem", kind="raster")

    result = ops.LayerListStyleCategories().execute_main(
        make_context(project),
        ops.LayerListStyleCategoriesParams(layer_id="dem"),
    )
    assert tuple(result.categories) == RASTER_CATEGORIES
    # 桌面版给栅格的勾选框里没有 Labeling。
    # Desktop's raster checkboxes do not include Labeling.
    assert "Labeling" not in result.categories


@pytest.mark.parametrize("operation", STYLE_READS)
def test_every_style_read_refuses_an_unknown_layer(
    fake_qgis: FakeQgis, data_root: Path, operation: Any
) -> None:
    ctx = make_context(FakeProject())
    assert refuse(operation(), ctx, style_request(operation, "missing")) == LAYER_NOT_FOUND


@pytest.mark.parametrize("operation", STYLE_READS)
def test_every_style_read_refuses_an_invalid_layer(
    fake_qgis: FakeQgis, data_root: Path, operation: Any
) -> None:
    project = FakeProject()
    broken = style_layer(fake_qgis, project, "broken")
    broken.valid = False

    ctx = make_context(project)
    assert refuse(operation(), ctx, style_request(operation, "broken")) == INVALID_LAYER


@pytest.mark.parametrize("operation", STYLE_READS)
def test_every_style_read_refuses_another_layer_kind(
    fake_qgis: FakeQgis, data_root: Path, operation: Any
) -> None:
    project = FakeProject()
    project.layers["plugin-1"] = FakeLayer("plugin-1", "annotations", provider="custom", authid="")

    ctx = make_context(project)
    assert refuse(operation(), ctx, style_request(operation, "plugin-1")) == INVALID_LAYER


def test_a_geometry_less_vector_table_keeps_its_style_reads(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """属性表也有字段、表单、属性表设置这些分类，也能导出 QML。

    A table still has fields, forms and attribute-table settings to export.
    """

    project = FakeProject()
    layer = style_layer(fake_qgis, project, "table")
    layer.geometry = 4  # 对应 Qgis.GeometryType.NullGeometry / Qgis.GeometryType.NullGeometry
    ctx = make_context(project)

    categories = ops.LayerListStyleCategories().execute_main(
        ctx, ops.LayerListStyleCategoriesParams(layer_id="table")
    )
    assert tuple(categories.categories) == VECTOR_CATEGORIES

    qml = ops.LayerGetQml().execute_main(ctx, ops.LayerGetQmlParams(layer_id="table"))
    assert qml.qml == layer.style_xml


def test_get_qml_without_categories_exports_the_whole_applicable_set(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "roads")
    layer.style_xml = '<!DOCTYPE qgis>\n<qgis styleCategories="AllStyleCategories"/>\n'

    result = ops.LayerGetQml().execute_main(
        make_context(project), ops.LayerGetQmlParams(layer_id="roads")
    )
    assert result.qml == layer.style_xml
    assert set(result.model_dump()) == {"layer_id", "qml"}
    context, flag = layer.style_exports[0]
    assert isinstance(context, fake_qgis.QgsReadWriteContext)
    assert flag == category_flag(fake_qgis, VECTOR_CATEGORIES)


def test_get_qml_uses_the_raster_set_for_a_raster_layer(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "dem", kind="raster")

    ops.LayerGetQml().execute_main(make_context(project), ops.LayerGetQmlParams(layer_id="dem"))
    assert layer.style_exports[0][1] == category_flag(fake_qgis, RASTER_CATEGORIES)


def test_get_qml_only_ors_the_requested_categories(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "roads")

    ops.LayerGetQml().execute_main(
        make_context(project),
        ops.LayerGetQmlParams(layer_id="roads", categories=["Symbology", "Labeling"]),
    )
    assert layer.style_exports[0][1] == category_flag(fake_qgis, ("Symbology", "Labeling"))


def test_get_qml_exports_a_single_category(fake_qgis: FakeQgis, data_root: Path) -> None:
    """一个分类走的是与多个分类不同的标志位路径。

    One category takes a different flag path than several.
    """

    project = FakeProject()
    layer = style_layer(fake_qgis, project, "roads")

    result = ops.LayerGetQml().execute_main(
        make_context(project),
        ops.LayerGetQmlParams(layer_id="roads", categories=["Symbology"]),
    )
    assert result.qml == layer.style_xml
    assert layer.style_exports[0][1] == category_flag(fake_qgis, ("Symbology",))


def test_get_qml_with_an_empty_category_list_exports_the_empty_set(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """ "一个都不选」是桌面版的 Deselect All + OK，不是错误。

    "None of them" is desktop's Deselect All plus OK, not an error.
    """

    assert ops.LayerGetQmlParams(layer_id="roads").categories is None
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "roads")

    result = ops.LayerGetQml().execute_main(
        make_context(project),
        ops.LayerGetQmlParams.model_validate({"layer_id": "roads", "categories": []}),
    )
    assert result.qml == layer.style_xml
    # 空标志就是原生那个“零分类”（实测 exportNamedStyle 接受它）。
    # The empty flag is the native "zero categories" (measured: exportNamedStyle takes it).
    assert layer.style_exports[0][1] == 0
    assert category_flag(fake_qgis, ()) == 0


def test_get_qml_refuses_a_repeating_category_list(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "roads")

    code = refuse(
        ops.LayerGetQml(),
        make_context(project),
        ops.LayerGetQmlParams(layer_id="roads", categories=["Symbology", "Symbology"]),
    )
    assert code == INVALID_INPUT
    assert layer.style_exports == []


def test_get_qml_refuses_a_category_the_layer_does_not_have(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "dem", kind="raster")

    code = refuse(
        ops.LayerGetQml(),
        make_context(project),
        ops.LayerGetQmlParams(layer_id="dem", categories=["Symbology3D"]),
    )
    assert code == INVALID_INPUT
    assert layer.style_exports == []


def test_an_unknown_category_name_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError):
        ops.LayerGetQmlParams.model_validate({"layer_id": "roads", "categories": ["Avaiable"]})


def test_a_failed_native_export_is_internal_error_without_the_native_message(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    # 3.44 用非空消息表示导出失败。
    # 而不是 (message, ok) 这一对。
    # A failed export is a non-empty message.
    # Not a (message, ok) pair.
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "roads")
    layer.style_error = "C:/secret/roads.qml: permission denied"

    with pytest.raises(FrameworkError) as caught:
        ops.LayerGetQml().execute_main(
            make_context(project), ops.LayerGetQmlParams(layer_id="roads")
        )
    assert caught.value.code == INTERNAL_ERROR
    assert "secret" not in caught.value.message


def test_an_exporter_that_raises_is_internal_error(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "roads")

    def explode(*_: Any) -> str:
        raise RuntimeError("C:/secret/roads.qml")

    layer.exportNamedStyle = explode
    with pytest.raises(FrameworkError) as caught:
        ops.LayerGetQml().execute_main(
            make_context(project), ops.LayerGetQmlParams(layer_id="roads")
        )
    assert caught.value.code == INTERNAL_ERROR
    assert "secret" not in caught.value.message


def test_the_vector_renderer_list_is_sorted_and_deduplicated(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    fake_qgis.vector_renderer_types = ["singleSymbol", "RuleRenderer", "singleSymbol"]
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "roads")

    result = ops.LayerListAvailableRenderers().execute_main(
        make_context(project),
        ops.LayerListAvailableRenderersParams(layer_id="roads"),
    )
    assert result.renderer_types == ["RuleRenderer", "singleSymbol"]
    assert set(result.model_dump()) == {"renderer_types"}
    assert fake_qgis.renderer_registry_layers == [layer]
    assert fake_qgis.raster_registries_built == 0


def test_the_raster_renderer_list_is_the_registry_filtered_by_the_raster_type(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """普通栅格的下拉框不列 ``singlebandcolordata``（qgsrasterlayerproperties.cpp:443-455）。

    An ordinary raster's combo does not list ``singlebandcolordata``.
    """

    fake_qgis.raster_renderer_types = [
        "singlecolor",
        "multibandcolor",
        "singlebandcolordata",
        "singlecolor",
    ]
    project = FakeProject()
    style_layer(fake_qgis, project, "dem", kind="raster")

    result = ops.LayerListAvailableRenderers().execute_main(
        make_context(project),
        ops.LayerListAvailableRenderersParams(layer_id="dem"),
    )
    assert result.renderer_types == ["multibandcolor", "singlecolor"]
    assert fake_qgis.renderer_registry_layers == []
    assert fake_qgis.raster_registries_built == 1


def test_a_single_band_color_data_raster_lists_only_that_renderer(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """``SingleBandColorData`` 栅格的下拉框里只有那一个（实测枚举值 3）。

    A ``SingleBandColorData`` raster's combo holds only that one (enum value 3, measured).
    """

    fake_qgis.raster_renderer_types = [
        "singlecolor",
        "multibandcolor",
        "singlebandcolordata",
    ]
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "rgba", kind="raster")
    layer.raster_type = 3

    result = ops.LayerListAvailableRenderers().execute_main(
        make_context(project),
        ops.LayerListAvailableRenderersParams(layer_id="rgba"),
    )
    assert result.renderer_types == ["singlebandcolordata"]


def test_get_renderer_reads_the_active_type_of_vector_and_raster(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    vector = style_layer(fake_qgis, project, "roads")
    vector.renderer_value = FakeRenderer("categorizedSymbol")
    raster = style_layer(fake_qgis, project, "dem", kind="raster")
    raster.renderer_value = FakeRenderer("singlebandgray")
    ctx = make_context(project)

    def renderer_type(layer_id: str) -> str:
        result = ops.LayerGetRenderer().execute_main(
            ctx, ops.LayerGetRendererParams(layer_id=layer_id)
        )
        return result.renderer_type

    assert renderer_type("roads") == "categorizedSymbol"
    assert renderer_type("dem") == "singlebandgray"


def test_a_spatial_layer_of_unknown_geometry_still_answers_the_renderer_reads(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """几何未知的空间图层有符号页，所以两个读都得给答案（旧判据会拒）。

    A spatial layer with unknown geometry has a symbology page, so both reads answer it
    (the old geometry whitelist refused it).
    """

    fake_qgis.vector_renderer_types = ["heatmapRenderer", "singleSymbol"]
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "unknown")
    layer.geometry = 3  # 对应 Qgis.GeometryType.Unknown / Qgis.GeometryType.Unknown
    layer.renderer_value = FakeRenderer("heatmapRenderer")
    ctx = make_context(project)

    renderer = ops.LayerGetRenderer().execute_main(
        ctx, ops.LayerGetRendererParams(layer_id="unknown")
    )
    assert renderer.renderer_type == "heatmapRenderer"

    listed = ops.LayerListAvailableRenderers().execute_main(
        ctx, ops.LayerListAvailableRenderersParams(layer_id="unknown")
    )
    assert listed.renderer_types == ["heatmapRenderer", "singleSymbol"]
    assert fake_qgis.renderer_registry_layers == [layer]


def test_the_setter_on_an_unknown_geometry_refuses_at_the_symbol_step(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """放行到符号那一步：空符号装得上，带符号的配置在几何上被拒。

    Admitted up to the symbol step: a null symbol installs, a real one is refused on the
    geometry.
    """

    project = FakeProject()
    layer = style_layer(fake_qgis, project, "unknown")
    layer.geometry = 3  # 对应 Qgis.GeometryType.Unknown / Qgis.GeometryType.Unknown
    ctx = make_context(project)

    installed = set_renderer(
        make_context(project), setter_payload("unknown", {"type": "nullSymbol"})
    )
    assert installed.renderer_type == "nullSymbol"

    code = refuse_renderer(ctx, setter_payload("unknown", {"type": "singleSymbol", "symbol": FILL}))
    assert code == INVALID_LAYER


@pytest.mark.parametrize(
    "operation",
    (
        ops.LayerListAvailableRenderers,
        ops.LayerGetRenderer,
        ops.LayerSetRenderer,
    ),
)
def test_every_renderer_operation_refuses_a_layer_without_a_renderer(
    fake_qgis: FakeQgis, data_root: Path, operation: Any
) -> None:
    """没有渲染器的图层（例如一张属性表）在桌面版里没有符号页。

    A layer without a renderer (a geometry-less table, measured) has no symbology page.
    """

    project = FakeProject()
    layer = style_layer(fake_qgis, project, "table")
    layer.geometry = 4  # 对应 Qgis.GeometryType.NullGeometry / Qgis.GeometryType.NullGeometry
    layer.renderer_value = None

    request = {
        ops.LayerSetRenderer: {"renderer": {"type": "nullSymbol"}},
    }.get(operation, {})
    code = refuse(operation(), make_context(project), style_request(operation, "table", **request))
    assert code == INVALID_LAYER
    assert layer.renderer_sets == []


@pytest.mark.parametrize(
    "model",
    [
        ops.LayerListStyleCategoriesParams,
        ops.LayerGetQmlParams,
        ops.LayerListAvailableRenderersParams,
        ops.LayerGetRendererParams,
    ],
)
def test_a_style_read_request_needs_a_layer_id_and_forbids_extras(model: Any) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({"layer_id": ""})
    with pytest.raises(ValidationError):
        model.model_validate({"layer_id": "roads", "editable": True})


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            ops.LayerListStyleCategoriesResult,
            {"layer_id": "roads", "categories": [], "editable": True},
        ),
        (ops.LayerGetQmlResult, {"layer_id": "roads", "qml": "", "categories": []}),
        (ops.LayerListAvailableRenderersResult, {"renderer_types": [], "count": 0}),
        (ops.LayerGetRendererResult, {"layer_id": "roads", "renderer_type": "", "config": {}}),
    ],
)
def test_a_style_read_result_forbids_an_extra_field(model: Any, payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_the_style_reads_register_as_main_thread_queries() -> None:
    registry = OperationRegistry()
    for operation in (
        ops.LayerListStyleCategories(),
        ops.LayerGetQml(),
        ops.LayerListAvailableRenderers(),
        ops.LayerGetRenderer(),
    ):
        registry.register(operation)

    assert [
        (spec.name, spec.kind, spec.strategy, spec.destructive) for spec in registry.list_specs()
    ] == [
        ("layer.get_qml", "query", "main_thread", False),
        ("layer.get_renderer", "query", "main_thread", False),
        ("layer.list_available_renderers", "query", "main_thread", False),
        ("layer.list_style_categories", "query", "main_thread", False),
    ]


# --- 渲染器 setter / the renderer setter ----------------------------------

POINT = 0
LINE = 1
POLYGON = 2
NULL_GEOMETRY = 4

FILL = {"type": "fill", "color": "#398F59"}
LINE_SYMBOL = {"type": "line", "color": "#D95843", "width_mm": 0.8}
MARKER = {"type": "marker", "color": "#FF0000"}

# 这个版本认得但不设置的七个矢量类型和四个栅格类型。
# Seven vector and four raster types.
UNSUPPORTED_RENDERER_TYPES = (
    "pointDisplacement",
    "pointCluster",
    "heatmapRenderer",
    "mergedFeatureRenderer",
    "invertedPolygonRenderer",
    "25dRenderer",
    "embeddedSymbol",
    "paletted",
    "singlebandcolordata",
    "hillshade",
    "contour",
)


def setter_layer(
    fake: FakeQgis,
    project: FakeProject,
    layer_id: str,
    kind: str = "vector",
    geometry: int | None = None,
    bands: int = 1,
) -> Any:
    layer = style_layer(fake, project, layer_id, kind=kind)
    if geometry is not None:
        layer.geometry = geometry
    layer.band_count = bands
    return layer


def setter_payload(layer_id: str, renderer: dict[str, Any]) -> dict[str, Any]:
    return {"layer_id": layer_id, "renderer": renderer}


def set_renderer(ctx: OperationContext, payload: dict[str, Any]) -> Any:
    return ops.LayerSetRenderer().execute_main(
        ctx, ops.LayerSetRendererParams.model_validate(payload)
    )


def refuse_renderer(ctx: OperationContext, payload: dict[str, Any]) -> str:
    return refuse(
        ops.LayerSetRenderer(),
        ctx,
        ops.LayerSetRendererParams.model_validate(payload),
    )


def test_a_null_symbol_renderer_is_installed_in_one_call(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "parcels", geometry=POLYGON)
    layer.renderer_value = FakeRenderer("singleSymbol")

    result = set_renderer(make_context(project), setter_payload("parcels", {"type": "nullSymbol"}))

    assert result.layer_id == "parcels"
    assert result.renderer_type == "nullSymbol"
    assert set(result.model_dump()) == {"layer_id", "renderer_type"}
    # 渲染器在图层看到它之前就已经完整。
    # 并且只装了一次。
    # The renderer was complete before the layer saw it.
    # And it was installed once.
    assert layer.renderer_sets == [fake_qgis.built[0]]
    assert layer.renderer_value is layer.renderer_sets[0]
    assert layer.events == ["construct:nullSymbol", "setRenderer", "repaint"]
    assert project.dirty == [True]


def test_a_single_symbol_renderer_takes_the_declared_properties(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "parcels", geometry=POLYGON)

    result = set_renderer(
        make_context(project),
        setter_payload(
            "parcels",
            {
                "type": "singleSymbol",
                "symbol": {
                    "type": "fill",
                    "color": "#6CAC83",
                    "outline_color": "#275E40",
                    "outline_width_mm": 0.5,
                },
            },
        ),
    )

    renderer = layer.renderer_sets[0]
    assert isinstance(renderer, fake_qgis.QgsSingleSymbolRenderer)
    assert renderer.symbol.kind == "fill"
    assert renderer.symbol.properties == {
        "color": "#6CAC83",
        "outline_color": "#275E40",
        "outline_width": 0.5,
    }
    assert result.renderer_type == "singleSymbol"
    assert layer.events == [
        "construct:fillSymbol",
        "construct:singleSymbol",
        "setRenderer",
        "repaint",
    ]


@pytest.mark.parametrize(
    ("geometry", "symbol", "kind"),
    [(POINT, MARKER, "marker"), (LINE, LINE_SYMBOL, "line"), (POLYGON, FILL, "fill")],
)
def test_each_geometry_takes_only_its_own_symbol_type(
    fake_qgis: FakeQgis, data_root: Path, geometry: int, symbol: dict[str, Any], kind: str
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "features", geometry=geometry)

    set_renderer(
        make_context(project),
        setter_payload("features", {"type": "singleSymbol", "symbol": symbol}),
    )

    built = layer.renderer_sets[0].symbol
    assert built.kind == kind
    assert built.properties["color"] == symbol["color"]


def test_categories_keep_the_declared_order_and_values(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "landuse", geometry=POLYGON)
    layer.field_list = [FakeField("landuse", numeric=False)]
    ctx = make_context(project)

    result = set_renderer(
        ctx,
        setter_payload(
            "landuse",
            {
                "type": "categorizedSymbol",
                "field": "landuse",
                "categories": [
                    {"value": "forest", "label": "Forest", "symbol": FILL},
                    {"value": "water", "label": "Water", "symbol": FILL},
                ],
            },
        ),
    )

    renderer = layer.renderer_sets[0]
    assert isinstance(renderer, fake_qgis.QgsCategorizedSymbolRenderer)
    assert renderer.field == "landuse"
    assert [(category.value, category.label) for category in renderer.categories] == [
        ("forest", "Forest"),
        ("water", "Water"),
    ]
    assert [category.symbol.kind for category in renderer.categories] == ["fill", "fill"]
    assert result.renderer_type == "categorizedSymbol"
    # 读侧和装上去的那个对象一致。
    # The read side agrees with the installed object.
    read_back = ops.LayerGetRenderer().execute_main(
        ctx, ops.LayerGetRendererParams(layer_id="landuse")
    )
    assert read_back.renderer_type == result.renderer_type


def test_ranges_keep_the_declared_order_and_limits(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "pop", geometry=POLYGON)
    layer.field_list = [FakeField("population", numeric=True)]

    result = set_renderer(
        make_context(project),
        setter_payload(
            "pop",
            {
                "type": "graduatedSymbol",
                "field": "population",
                "ranges": [
                    {"lower": 0, "upper": 1000, "label": "Low", "symbol": FILL},
                    {"lower": 1000, "upper": 5000, "label": "High", "symbol": FILL},
                ],
            },
        ),
    )

    renderer = layer.renderer_sets[0]
    assert isinstance(renderer, fake_qgis.QgsGraduatedSymbolRenderer)
    assert renderer.field == "population"
    assert [(item.lower, item.upper, item.label) for item in renderer.ranges] == [
        (0.0, 1000.0, "Low"),
        (1000.0, 5000.0, "High"),
    ]
    assert result.renderer_type == "graduatedSymbol"


def test_a_flat_rule_renderer_builds_its_rules_in_order(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "roads", geometry=LINE)

    result = set_renderer(
        make_context(project),
        setter_payload(
            "roads",
            {
                "type": "RuleRenderer",
                "rules": [
                    {"label": "Main", "filter": "\"class\" = 'primary'", "symbol": LINE_SYMBOL},
                    {
                        "label": "Other",
                        "filter": "\"class\" = 'secondary'",
                        "symbol": LINE_SYMBOL,
                    },
                    {"label": "Else", "else_rule": True, "symbol": LINE_SYMBOL},
                ],
            },
        ),
    )

    renderer = layer.renderer_sets[0]
    assert isinstance(renderer, fake_qgis.QgsRuleBasedRenderer)
    children = renderer.root.children
    # 用 Rule(None) 造的根一开始是空的。
    # 没有隐式的兜底规则。
    # A root built from Rule(None) starts empty.
    # No implicit catch-all rule exists.
    assert [child.label for child in children] == ["Main", "Other", "Else"]
    assert [child.filter for child in children] == [
        "\"class\" = 'primary'",
        "\"class\" = 'secondary'",
        "",
    ]
    assert [child.else_rule for child in children] == [False, False, True]
    assert all(child.active is True for child in children)
    assert [child.symbol.kind for child in children] == ["line", "line", "line"]
    assert result.renderer_type == "RuleRenderer"
    assert layer.events == [
        "construct:lineSymbol",
        "construct:lineSymbol",
        "construct:lineSymbol",
        "construct:RuleRenderer",
        "setRenderer",
        "repaint",
    ]


def test_a_single_band_gray_renderer_carries_its_stretch(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "dem", kind="raster", bands=4)

    result = set_renderer(
        make_context(project),
        setter_payload("dem", {"type": "singlebandgray", "band": 2, "min": 0, "max": 2500}),
    )

    renderer = layer.renderer_sets[0]
    assert isinstance(renderer, fake_qgis.QgsSingleBandGrayRenderer)
    assert renderer.band == 2
    assert renderer.provider is layer.data_provider
    enhancement = renderer.enhancement
    assert enhancement is not None
    # 增强用波段自己的数据类型。
    # 拉伸值是声明的那些。
    # The enhancement takes the band's own data type.
    # The stretch is the declared one.
    assert enhancement.data_type == fake_qgis.raster_data_type
    assert (enhancement.minimum, enhancement.maximum) == (0.0, 2500.0)
    assert enhancement.algorithm == fake_qgis.QgsContrastEnhancement.StretchToMinimumMaximum
    # 两个边界只存值。
    # 表由算法 setter 建一次。
    # The bounds only store their values.
    # The algorithm setter builds the table once.
    assert (enhancement.minimum_table, enhancement.maximum_table) == (False, False)
    assert layer.events == [
        "construct:singlebandgray",
        "minimum",
        "maximum",
        "algorithm",
        "contrast",
        "setRenderer",
        "repaint",
    ]
    assert result.renderer_type == "singlebandgray"


def test_a_multiband_renderer_may_repeat_a_band(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "satellite", kind="raster", bands=4)

    result = set_renderer(
        make_context(project),
        setter_payload(
            "satellite", {"type": "multibandcolor", "red_band": 4, "green_band": 4, "blue_band": 2}
        ),
    )

    renderer = layer.renderer_sets[0]
    assert isinstance(renderer, fake_qgis.QgsMultiBandColorRenderer)
    assert (renderer.red, renderer.green, renderer.blue) == (4, 4, 2)
    assert renderer.provider is layer.data_provider
    assert result.renderer_type == "multibandcolor"


@pytest.mark.parametrize(
    ("interpolation", "ramp_type"),
    [("linear", "Interpolated"), ("discrete", "Discrete"), ("exact", "Exact")],
)
def test_a_pseudocolor_renderer_carries_its_stops(
    fake_qgis: FakeQgis, data_root: Path, interpolation: str, ramp_type: str
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "dem", kind="raster", bands=1)

    result = set_renderer(
        make_context(project),
        setter_payload(
            "dem",
            {
                "type": "singlebandpseudocolor",
                "band": 1,
                "interpolation": interpolation,
                "stops": [
                    {"value": 0, "color": "#2665B8"},
                    {"value": 1000, "color": "#F2E9A4"},
                    {"value": 2500, "color": "#B44A39"},
                ],
            },
        ),
    )

    renderer = layer.renderer_sets[0]
    assert isinstance(renderer, fake_qgis.QgsSingleBandPseudoColorRenderer)
    assert renderer.band == 1
    ramp = renderer.shader.function
    assert isinstance(ramp, fake_qgis.QgsColorRampShader)
    assert ramp.ramp_type == getattr(fake_qgis.QgsColorRampShader, ramp_type)
    assert [(item.value, item.color.name()) for item in ramp.items] == [
        (0.0, "#2665b8"),
        (1000.0, "#f2e9a4"),
        (2500.0, "#b44a39"),
    ]
    assert result.renderer_type == "singlebandpseudocolor"


def test_a_single_color_renderer_carries_its_colour(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "mask", kind="raster", bands=1)

    result = set_renderer(
        make_context(project),
        setter_payload("mask", {"type": "singlecolor", "band": 1, "color": "#CC6633"}),
    )

    renderer = layer.renderer_sets[0]
    assert isinstance(renderer, fake_qgis.QgsRasterSingleColorRenderer)
    assert (renderer.band, renderer.color.name()) == (1, "#cc6633")
    assert result.renderer_type == "singlecolor"


def test_a_native_constructor_that_raises_leaves_the_old_renderer(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "landuse", geometry=POLYGON)
    layer.field_list = [FakeField("landuse", numeric=False)]
    old = FakeRenderer("singleSymbol")
    layer.renderer_value = old
    fake_qgis.fail_build = "categorizedSymbol"

    with pytest.raises(FrameworkError) as caught:
        set_renderer(
            make_context(project),
            setter_payload(
                "landuse",
                {
                    "type": "categorizedSymbol",
                    "field": "landuse",
                    "categories": [{"value": "forest", "label": "Forest", "symbol": FILL}],
                },
            ),
        )

    assert caught.value.code == INTERNAL_ERROR
    assert "setRenderer" not in fake_qgis.events
    assert layer.renderer_sets == []
    assert layer.renderer_value is old
    assert project.dirty == []


@pytest.mark.parametrize("renderer_type", UNSUPPORTED_RENDERER_TYPES)
def test_an_unimplemented_renderer_type_never_touches_the_layer(
    fake_qgis: FakeQgis, data_root: Path, renderer_type: str
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "parcels", geometry=POLYGON)
    old = FakeRenderer("singleSymbol")
    layer.renderer_value = old

    # union 拒掉这个类型。
    # 引擎把它报成 INVALID_INPUT。
    # 没有任何图层被碰到，所以什么都没变。
    # The union refuses the type.
    # The engine reports INVALID_INPUT.
    # No layer is reached, so nothing changed.
    with pytest.raises(ValidationError):
        ops.LayerSetRendererParams.model_validate(
            setter_payload("parcels", {"type": renderer_type})
        )
    assert layer.renderer_sets == []
    assert layer.renderer_value is old


@pytest.mark.parametrize(
    ("geometry", "symbol"),
    [(POINT, FILL), (LINE, MARKER), (POLYGON, LINE_SYMBOL)],
)
def test_a_symbol_for_another_geometry_is_refused(
    fake_qgis: FakeQgis, data_root: Path, geometry: int, symbol: dict[str, Any]
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "features", geometry=geometry)

    code = refuse_renderer(
        make_context(project),
        setter_payload("features", {"type": "singleSymbol", "symbol": symbol}),
    )
    assert code == INVALID_INPUT
    assert layer.renderer_sets == []


@pytest.mark.parametrize("color", ["red", "#ff000", "#ff0000ff", "#gggggg", ""])
def test_a_colour_must_be_six_rgb_hex_digits(data_root: Path, color: str) -> None:
    with pytest.raises(ValidationError):
        ops.LayerSetRendererParams.model_validate(
            setter_payload(
                "parcels", {"type": "singleSymbol", "symbol": {"type": "fill", "color": color}}
            )
        )


@pytest.mark.parametrize(
    ("symbol", "field_name"),
    [
        ({"type": "marker", "color": "#FF0000"}, "size_mm"),
        ({"type": "line", "color": "#FF0000"}, "width_mm"),
        ({"type": "fill", "color": "#FF0000"}, "outline_width_mm"),
    ],
)
@pytest.mark.parametrize("value", [0, -0.5, float("nan"), float("inf")])
def test_a_symbol_length_must_be_positive_and_finite(
    data_root: Path, symbol: dict[str, Any], field_name: str, value: float
) -> None:
    with pytest.raises(ValidationError):
        ops.LayerSetRendererParams.model_validate(
            setter_payload(
                "features",
                {"type": "singleSymbol", "symbol": {**symbol, field_name: value}},
            )
        )


def test_a_missing_category_field_is_invalid_field_type(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "landuse", geometry=POLYGON)

    code = refuse_renderer(
        make_context(project),
        setter_payload(
            "landuse",
            {
                "type": "categorizedSymbol",
                "field": "missing",
                "categories": [{"value": "forest", "label": "Forest", "symbol": FILL}],
            },
        ),
    )
    assert code == INVALID_FIELD_TYPE
    assert layer.renderer_sets == []


def test_a_repeating_category_value_is_refused(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "landuse", geometry=POLYGON)
    layer.field_list = [FakeField("landuse", numeric=False)]

    code = refuse_renderer(
        make_context(project),
        setter_payload(
            "landuse",
            {
                "type": "categorizedSymbol",
                "field": "landuse",
                "categories": [
                    {"value": "forest", "label": "Forest", "symbol": FILL},
                    {"value": "forest", "label": "Forest again", "symbol": FILL},
                ],
            },
        ),
    )
    assert code == INVALID_INPUT
    assert layer.renderer_sets == []


def test_two_null_categories_are_refused(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "landuse", geometry=POLYGON)
    layer.field_list = [FakeField("landuse", numeric=False)]

    code = refuse_renderer(
        make_context(project),
        setter_payload(
            "landuse",
            {
                "type": "categorizedSymbol",
                "field": "landuse",
                "categories": [
                    {"value": None, "label": "Unset", "symbol": FILL},
                    {"value": None, "label": "Also unset", "symbol": FILL},
                ],
            },
        ),
    )
    assert code == INVALID_INPUT
    assert layer.renderer_sets == []


def test_a_numeric_field_normalises_one_and_one_point_zero(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "pop", geometry=POLYGON)
    layer.field_list = [FakeField("population", numeric=True)]

    code = refuse_renderer(
        make_context(project),
        setter_payload(
            "pop",
            {
                "type": "categorizedSymbol",
                "field": "population",
                "categories": [
                    {"value": 1, "label": "One", "symbol": FILL},
                    {"value": 1.0, "label": "One again", "symbol": FILL},
                ],
            },
        ),
    )
    assert code == INVALID_INPUT
    assert layer.renderer_sets == []


def test_a_fractional_value_on_an_integer_field_is_refused(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """字段根本存不下的分类值会让图层整片空白，所以拒掉。

    A category the field can never hold would blank the layer, so it is refused.
    """

    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "num", geometry=POLYGON)
    layer.field_list = [FakeField("num", field_type=fake_qgis.QVariant.Int)]
    old = FakeRenderer("singleSymbol")
    layer.renderer_value = old

    code = refuse_renderer(
        make_context(project),
        setter_payload(
            "num",
            {
                "type": "categorizedSymbol",
                "field": "num",
                "categories": [{"value": 2.5, "label": "Midpoint", "symbol": FILL}],
            },
        ),
    )
    assert code == INVALID_INPUT
    assert fake_qgis.built == []
    assert layer.renderer_sets == []
    assert layer.renderer_value is old


def test_a_whole_value_on_an_integer_field_is_stored_as_an_integer(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """``2.0`` 是这个整数字段存得下的值，存下来的值也说明这一点。

    ``2.0`` is a value the integer field can hold, and the stored value says so.
    """

    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "num", geometry=POLYGON)
    layer.field_list = [FakeField("num", field_type=fake_qgis.QVariant.Int)]

    result = set_renderer(
        make_context(project),
        setter_payload(
            "num",
            {
                "type": "categorizedSymbol",
                "field": "num",
                "categories": [{"value": 2.0, "label": "Two", "symbol": FILL}],
            },
        ),
    )

    renderer = layer.renderer_sets[0]
    assert result.renderer_type == "categorizedSymbol"
    assert type(renderer.categories[0].value) is int
    assert renderer.categories[0].value == 2
    assert renderer.categoryIndexForValue(2) == 0


def test_a_whole_float_and_an_integer_are_one_category_on_an_integer_field(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """两种写法仍是同一个分类：去重键不看类型。

    The two spellings stay one category: the dedupe key does not depend on the type.
    """

    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "num", geometry=POLYGON)
    layer.field_list = [FakeField("num", field_type=fake_qgis.QVariant.Int)]

    code = refuse_renderer(
        make_context(project),
        setter_payload(
            "num",
            {
                "type": "categorizedSymbol",
                "field": "num",
                "categories": [
                    {"value": 2, "label": "Two", "symbol": FILL},
                    {"value": 2.0, "label": "Two again", "symbol": FILL},
                ],
            },
        ),
    )
    assert code == INVALID_INPUT
    assert layer.renderer_sets == []


@pytest.mark.parametrize(
    ("variant_name", "value"),
    [
        ("Int", 2**31),
        ("Int", -(2**31) - 1),
        ("Int", 2**40),
        ("LongLong", 2**63),
        ("LongLong", -(2**63) - 1),
        ("LongLong", 10**19),
    ],
)
def test_an_integer_outside_the_fields_range_is_refused(
    fake_qgis: FakeQgis, data_root: Path, variant_name: str, value: int
) -> None:
    """这种值不可能是要素的值，分类永远匹配不上。

    No such value can be a feature, so the category would never match.
    """

    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "num", geometry=POLYGON)
    layer.field_list = [FakeField("num", field_type=getattr(fake_qgis.QVariant, variant_name))]
    old = FakeRenderer("singleSymbol")
    layer.renderer_value = old

    code = refuse_renderer(
        make_context(project),
        setter_payload(
            "num",
            {
                "type": "categorizedSymbol",
                "field": "num",
                "categories": [{"value": value, "label": "Out", "symbol": FILL}],
            },
        ),
    )
    assert code == INVALID_INPUT
    assert fake_qgis.built == []
    assert layer.renderer_sets == []
    assert layer.renderer_value is old


@pytest.mark.parametrize(
    ("variant_name", "value"),
    [
        ("Int", 2**31 - 1),
        ("Int", -(2**31)),
        ("LongLong", 2**63 - 1),
        ("LongLong", -(2**63)),
    ],
)
def test_an_integer_at_the_fields_edge_is_stored_as_an_integer(
    fake_qgis: FakeQgis, data_root: Path, variant_name: str, value: int
) -> None:
    """字段能存的最外层值仍然接受，并且原生也能匹配上。

    The outermost values the field can hold stay accepted and match natively.
    """

    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "num", geometry=POLYGON)
    layer.field_list = [FakeField("num", field_type=getattr(fake_qgis.QVariant, variant_name))]

    set_renderer(
        make_context(project),
        setter_payload(
            "num",
            {
                "type": "categorizedSymbol",
                "field": "num",
                "categories": [{"value": value, "label": "Edge", "symbol": FILL}],
            },
        ),
    )

    category = layer.renderer_sets[0].categories[0]
    assert type(category.value) is int
    assert category.value == value
    assert layer.renderer_sets[0].categoryIndexForValue(value) == 0


def test_two_integers_one_float_cannot_tell_apart_are_two_categories(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """超过 2**53 后两个 int64 值共用一个 float，但字段仍然装得下两个。

    Above 2**53 two int64 values share a float; the field still holds both.
    """

    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "num", geometry=POLYGON)
    layer.field_list = [FakeField("num", field_type=fake_qgis.QVariant.LongLong)]
    shared = 2**53
    neighbour = shared + 1

    set_renderer(
        make_context(project),
        setter_payload(
            "num",
            {
                "type": "categorizedSymbol",
                "field": "num",
                "categories": [
                    {"value": shared, "label": "Shared", "symbol": FILL},
                    {"value": neighbour, "label": "Neighbour", "symbol": FILL},
                ],
            },
        ),
    )

    renderer = layer.renderer_sets[0]
    assert [category.value for category in renderer.categories] == [shared, neighbour]
    assert all(type(category.value) is int for category in renderer.categories)
    assert renderer.categoryIndexForValue(neighbour) == 1


def test_a_category_value_the_field_cannot_hold_is_refused(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "pop", geometry=POLYGON)
    layer.field_list = [FakeField("population", numeric=True)]

    code = refuse_renderer(
        make_context(project),
        setter_payload(
            "pop",
            {
                "type": "categorizedSymbol",
                "field": "population",
                "categories": [{"value": "one", "label": "One", "symbol": FILL}],
            },
        ),
    )
    assert code == INVALID_INPUT
    assert layer.renderer_sets == []


def test_an_unrepresentable_category_value_is_refused(fake_qgis: FakeQgis, data_root: Path) -> None:
    """装不进 float 的整数是坏值，不是内部故障。

    An int too large for a float is a bad value, not an internal failure.
    """

    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "pop", geometry=POLYGON)
    layer.field_list = [FakeField("population", numeric=True)]

    code = refuse_renderer(
        make_context(project),
        setter_payload(
            "pop",
            {
                "type": "categorizedSymbol",
                "field": "population",
                "categories": [{"value": 10**400, "label": "Huge", "symbol": FILL}],
            },
        ),
    )
    assert code == INVALID_INPUT
    assert fake_qgis.built == []
    assert layer.renderer_sets == []


def test_a_boolean_category_is_refused_on_a_field_that_is_not_boolean(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """日期字段上的 bool 会变成永远匹配不上的分类。

    A bool on a date field would be a category that can never match.
    """

    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "events", geometry=POLYGON)
    layer.field_list = [FakeField("recorded", numeric=False, field_type=fake_qgis.QVariant.Date)]

    code = refuse_renderer(
        make_context(project),
        setter_payload(
            "events",
            {
                "type": "categorizedSymbol",
                "field": "recorded",
                "categories": [{"value": True, "label": "True", "symbol": FILL}],
            },
        ),
    )
    assert code == INVALID_INPUT
    assert fake_qgis.built == []
    assert layer.renderer_sets == []


def test_a_boolean_category_is_accepted_on_a_boolean_field(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "flags", geometry=POLYGON)
    layer.field_list = [FakeField("active", numeric=False, field_type=fake_qgis.QVariant.Bool)]

    result = set_renderer(
        make_context(project),
        setter_payload(
            "flags",
            {
                "type": "categorizedSymbol",
                "field": "active",
                "categories": [{"value": True, "label": "On", "symbol": FILL}],
            },
        ),
    )
    assert layer.renderer_sets[0].categories[0].value is True
    assert result.renderer_type == "categorizedSymbol"


@pytest.mark.parametrize("value", ["true", "True", "yes", "1"], ids=repr)
def test_a_string_a_boolean_field_converts_is_written_as_that_boolean(
    fake_qgis: FakeQgis, data_root: Path, value: str
) -> None:
    """渲染器存的是转换后的值，所以要素才匹配得上。

    The converted value is what the renderer stores, so a feature can match it.
    """
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "flags", geometry=POLYGON)
    layer.field_list = [FakeField("active", numeric=False, field_type=fake_qgis.QVariant.Bool)]

    set_renderer(
        make_context(project),
        setter_payload(
            "flags",
            {
                "type": "categorizedSymbol",
                "field": "active",
                "categories": [{"value": value, "label": "On", "symbol": FILL}],
            },
        ),
    )
    assert layer.renderer_sets[0].categories[0].value is True


def test_a_boolean_and_its_string_form_are_one_category(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """同一个值的两种写法按转换后的值去重，而不是按原始值。

    Two spellings dedupe on the converted value, not the raw one.
    """
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "flags", geometry=POLYGON)
    layer.field_list = [FakeField("active", numeric=False, field_type=fake_qgis.QVariant.Bool)]

    code = refuse_renderer(
        make_context(project),
        setter_payload(
            "flags",
            {
                "type": "categorizedSymbol",
                "field": "active",
                "categories": [
                    {"value": True, "label": "On", "symbol": FILL},
                    {"value": "true", "label": "Also on", "symbol": FILL},
                ],
            },
        ),
    )
    assert code == INVALID_INPUT
    assert layer.renderer_sets == []


def test_a_date_field_takes_a_date_string_and_refuses_a_non_date(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """日期分类必须转成日期，否则永远匹配不上要素。

    A date category has to convert to a date, or it can never match a feature.
    """
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "events", geometry=POLYGON)
    layer.field_list = [FakeField("recorded", numeric=False, field_type=fake_qgis.QVariant.Date)]
    ctx = make_context(project)
    payload = {
        "type": "categorizedSymbol",
        "field": "recorded",
        "categories": [{"value": "2024-01-01", "label": "First", "symbol": FILL}],
    }

    set_renderer(ctx, setter_payload("events", payload))
    assert layer.renderer_sets[0].categories[0].value == date(2024, 1, 1)

    payload["categories"] = [{"value": "nope", "label": "Broken", "symbol": FILL}]
    assert refuse_renderer(ctx, setter_payload("events", payload)) == INVALID_INPUT
    assert len(layer.renderer_sets) == 1


def test_a_missing_graduated_field_is_invalid_field_type(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "pop", geometry=POLYGON)

    code = refuse_renderer(
        make_context(project),
        setter_payload(
            "pop",
            {
                "type": "graduatedSymbol",
                "field": "missing",
                "ranges": [{"lower": 0, "upper": 1, "label": "Low", "symbol": FILL}],
            },
        ),
    )
    assert code == INVALID_FIELD_TYPE
    assert layer.renderer_sets == []


def test_a_non_numeric_graduated_field_is_invalid_field_type(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "landuse", geometry=POLYGON)
    layer.field_list = [FakeField("landuse", numeric=False)]

    code = refuse_renderer(
        make_context(project),
        setter_payload(
            "landuse",
            {
                "type": "graduatedSymbol",
                "field": "landuse",
                "ranges": [{"lower": 0, "upper": 1, "label": "Low", "symbol": FILL}],
            },
        ),
    )
    assert code == INVALID_FIELD_TYPE
    assert layer.renderer_sets == []


@pytest.mark.parametrize(
    "ranges",
    [
        [
            {"lower": 0, "upper": 10, "label": "A", "symbol": FILL},
            {"lower": 5, "upper": 15, "label": "B", "symbol": FILL},
        ],
        [
            {"lower": 10, "upper": 20, "label": "A", "symbol": FILL},
            {"lower": 0, "upper": 5, "label": "B", "symbol": FILL},
        ],
    ],
)
def test_overlapping_or_unordered_ranges_are_refused(ranges: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        ops.GraduatedRendererConfig.model_validate(
            {"type": "graduatedSymbol", "field": "pop", "ranges": ranges}
        )


def test_ranges_may_share_an_endpoint() -> None:
    config = ops.GraduatedRendererConfig.model_validate(
        {
            "type": "graduatedSymbol",
            "field": "pop",
            "ranges": [
                {"lower": 0, "upper": 10, "label": "A", "symbol": FILL},
                {"lower": 10, "upper": 20, "label": "B", "symbol": FILL},
            ],
        }
    )
    assert len(config.ranges) == 2


def test_a_range_needs_lower_below_upper() -> None:
    with pytest.raises(ValidationError):
        ops.RangeConfig.model_validate({"lower": 5, "upper": 5, "label": "A", "symbol": FILL})


def test_a_range_label_may_be_empty() -> None:
    """标签只要求是字符串；没有文字的图例条目也合法。

    A label string is all that is asked; a legend entry without text is legal.
    """

    config = ops.RangeConfig.model_validate({"lower": 0, "upper": 10, "label": "", "symbol": FILL})
    assert config.label == ""


@pytest.mark.parametrize(
    "rules",
    [
        # ELSE 夹在中间。
        # An ELSE in the middle.
        [
            {"label": "Else", "else_rule": True, "symbol": FILL},
            {"label": "A", "filter": '"a" = 1', "symbol": FILL},
        ],
        # 两个 ELSE：只有最后一个可以是。
        # Two ELSE rules: only the last may be one.
        [
            {"label": "Else", "else_rule": True, "symbol": FILL},
            {"label": "Else again", "else_rule": True, "symbol": FILL},
        ],
        # ELSE 还带了过滤表达式。
        # An ELSE that also carries a filter.
        [{"label": "Else", "filter": '"a" = 1', "else_rule": True, "symbol": FILL}],
    ],
)
def test_an_else_rule_must_be_last_and_alone(rules: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        ops.RuleRendererConfig.model_validate({"type": "RuleRenderer", "rules": rules})


def test_a_rule_needs_a_filter_expression() -> None:
    with pytest.raises(ValidationError):
        ops.RuleRendererConfig.model_validate(
            {"type": "RuleRenderer", "rules": [{"label": "All", "symbol": FILL}]}
        )


def test_an_invalid_expression_is_refused_before_the_layer_is_touched(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "roads", geometry=LINE)
    fake_qgis.invalid_expressions.add('"class" =')

    code = refuse_renderer(
        make_context(project),
        setter_payload(
            "roads",
            {
                "type": "RuleRenderer",
                "rules": [{"label": "Main", "filter": '"class" =', "symbol": LINE_SYMBOL}],
            },
        ),
    )
    assert code == INVALID_INPUT
    assert fake_qgis.built == []
    assert layer.renderer_sets == []


@pytest.mark.parametrize(
    "renderer",
    [
        {"type": "singlebandgray", "band": 3, "min": 0, "max": 1},
        {"type": "multibandcolor", "red_band": 3, "green_band": 1, "blue_band": 1},
        {
            "type": "singlebandpseudocolor",
            "band": 3,
            "stops": [{"value": 0, "color": "#000000"}, {"value": 1, "color": "#FFFFFF"}],
        },
        {"type": "singlecolor", "band": 3, "color": "#CC6633"},
    ],
)
def test_a_band_outside_the_raster_is_refused(
    fake_qgis: FakeQgis, data_root: Path, renderer: dict[str, Any]
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "dem", kind="raster", bands=2)

    code = refuse_renderer(make_context(project), setter_payload("dem", renderer))
    assert code == INVALID_INPUT
    assert fake_qgis.built == []
    assert layer.renderer_sets == []


def test_band_zero_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError):
        ops.SingleBandGrayConfig.model_validate(
            {"type": "singlebandgray", "band": 0, "min": 0, "max": 1}
        )


def test_gray_min_must_be_below_max() -> None:
    with pytest.raises(ValidationError):
        ops.SingleBandGrayConfig.model_validate(
            {"type": "singlebandgray", "band": 1, "min": 5, "max": 5}
        )


@pytest.mark.parametrize(
    "stops",
    [
        [{"value": 0, "color": "#000000"}],
        [{"value": 0, "color": "#000000"}, {"value": 0, "color": "#FFFFFF"}],
        [{"value": 10, "color": "#000000"}, {"value": 5, "color": "#FFFFFF"}],
        [{"value": 0, "color": "#000000"}, {"value": float("nan"), "color": "#FFFFFF"}],
    ],
)
def test_pseudocolor_stops_must_strictly_increase(stops: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        ops.PseudoColorConfig.model_validate(
            {"type": "singlebandpseudocolor", "band": 1, "stops": stops}
        )


def test_the_setter_refuses_a_layer_that_does_not_exist(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    code = refuse_renderer(
        make_context(FakeProject()),
        setter_payload("missing", {"type": "nullSymbol"}),
    )
    assert code == LAYER_NOT_FOUND


def test_the_setter_refuses_an_invalid_layer(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "broken", geometry=POLYGON)
    layer.valid = False

    code = refuse_renderer(make_context(project), setter_payload("broken", {"type": "nullSymbol"}))
    assert code == INVALID_LAYER
    assert layer.renderer_sets == []


def test_the_setter_refuses_a_layer_without_a_renderer(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """没有渲染器的图层（例如一张属性表）在桌面版里没有符号页。

    A layer without a renderer (a geometry-less table, measured) has no symbology page.
    """

    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "table", geometry=NULL_GEOMETRY)
    layer.renderer_value = None

    code = refuse_renderer(make_context(project), setter_payload("table", {"type": "nullSymbol"}))
    assert code == INVALID_LAYER
    assert layer.renderer_sets == []


def test_a_raster_renderer_does_not_apply_to_a_vector_layer(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "parcels", geometry=POLYGON)

    code = refuse_renderer(
        make_context(project),
        setter_payload("parcels", {"type": "singlecolor", "band": 1, "color": "#CC6633"}),
    )
    assert code == INVALID_LAYER
    assert layer.renderer_sets == []


def test_a_vector_renderer_does_not_apply_to_a_raster_layer(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = setter_layer(fake_qgis, project, "dem", kind="raster")

    code = refuse_renderer(
        make_context(project),
        setter_payload("dem", {"type": "singleSymbol", "symbol": FILL}),
    )
    assert code == INVALID_LAYER
    assert layer.renderer_sets == []


SETTER_MODELS = [
    (ops.MarkerSymbolConfig, MARKER),
    (ops.LineSymbolConfig, LINE_SYMBOL),
    (ops.FillSymbolConfig, FILL),
    (ops.NullSymbolRendererConfig, {"type": "nullSymbol"}),
    (ops.SingleSymbolRendererConfig, {"type": "singleSymbol", "symbol": FILL}),
    (
        ops.CategorizedRendererConfig,
        {
            "type": "categorizedSymbol",
            "field": "landuse",
            "categories": [{"value": "forest", "label": "Forest", "symbol": FILL}],
        },
    ),
    (
        ops.GraduatedRendererConfig,
        {
            "type": "graduatedSymbol",
            "field": "pop",
            "ranges": [{"lower": 0, "upper": 1, "label": "Low", "symbol": FILL}],
        },
    ),
    (
        ops.RuleRendererConfig,
        {"type": "RuleRenderer", "rules": [{"label": "All", "filter": "1 = 1", "symbol": FILL}]},
    ),
    (ops.SingleBandGrayConfig, {"type": "singlebandgray", "band": 1, "min": 0, "max": 1}),
    (
        ops.MultiBandColorConfig,
        {"type": "multibandcolor", "red_band": 1, "green_band": 1, "blue_band": 1},
    ),
    (
        ops.PseudoColorConfig,
        {
            "type": "singlebandpseudocolor",
            "band": 1,
            "stops": [{"value": 0, "color": "#000000"}, {"value": 1, "color": "#FFFFFF"}],
        },
    ),
    (ops.SingleColorRasterConfig, {"type": "singlecolor", "band": 1, "color": "#CC6633"}),
    (ops.CategoryConfig, {"value": "forest", "label": "Forest", "symbol": FILL}),
    (ops.RangeConfig, {"lower": 0, "upper": 1, "label": "Low", "symbol": FILL}),
    (ops.RuleConfig, {"label": "All", "filter": "1 = 1", "symbol": FILL}),
    (ops.ColorStop, {"value": 0, "color": "#FFFFFF"}),
    (ops.LayerSetRendererParams, setter_payload("roads", {"type": "nullSymbol"})),
    (ops.LayerSetRendererResult, {"layer_id": "roads", "renderer_type": "nullSymbol"}),
]


@pytest.mark.parametrize(("model", "payload"), SETTER_MODELS)
def test_every_setter_model_forbids_an_extra_field(model: Any, payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "surprise": 1})


def test_the_renderer_setter_registers_as_a_destructive_command() -> None:
    registry = OperationRegistry()
    registry.register(ops.LayerSetRenderer())

    assert [
        (spec.name, spec.kind, spec.strategy, spec.destructive) for spec in registry.list_specs()
    ] == [("layer.set_renderer", "command", "main_thread", True)]
