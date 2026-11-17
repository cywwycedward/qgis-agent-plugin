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
import re
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest  # pyright: ignore[reportMissingImports]
from pydantic import ValidationError  # pyright: ignore[reportMissingImports]

from qgis_agent_plugin.core.config import Settings  # pyright: ignore[reportMissingImports]
from qgis_agent_plugin.core.contracts import OperationContext  # pyright: ignore[reportMissingImports]
from qgis_agent_plugin.core.models import (  # pyright: ignore[reportMissingImports]
    INTERNAL_ERROR,
    INVALID_INPUT,
    INVALID_LAYER,
    LAYER_NOT_FOUND,
    LAYER_NOT_IN_TREE,
    FrameworkError,
)
from qgis_agent_plugin.core.registry import OperationRegistry  # pyright: ignore[reportMissingImports]
from qgis_agent_plugin.operations import layer as ops  # pyright: ignore[reportMissingImports]

# 每个栅格渲染器类型名对应的原生类名。
# The native class name each raster renderer type name corresponds to.
RASTER_RENDERER_CLASS_NAMES = {
    "multibandcolor": "QgsMultiBandColorRenderer",
    "paletted": "QgsPalettedRasterRenderer",
    "singlebandgray": "QgsSingleBandGrayRenderer",
    "singlebandpseudocolor": "QgsSingleBandPseudoColorRenderer",
    "singlebandcolordata": "QgsSingleBandColorDataRenderer",
    "singlecolor": "QgsRasterSingleColorRenderer",
    "hillshade": "QgsHillshadeRenderer",
    "contour": "QgsRasterContourRenderer",
}


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
    """有渲染器的那一点：类型名，以及它自己那份文档。

    Enough of a renderer: the type name and the renderer's own document.
    """

    def __init__(self, renderer_type: str) -> None:
        self._renderer_type = renderer_type

    def type(self) -> str:
        return self._renderer_type

    def save(self, document: Any, context: Any) -> Any:
        element = document.createElement("renderer-v2")
        element.attributes["type"] = self._renderer_type
        return element

    def writeXml(self, document: Any, element: Any) -> None:
        # 栅格渲染器把自己写进容器的 ``rasterrenderer`` 子元素。
        # A raster renderer writes itself as a ``rasterrenderer`` child of the container.
        child = document.createElement("rasterrenderer")
        child.attributes["type"] = self._renderer_type
        element.appendChild(child)


class FakeElement:
    """一个元素的标签与属性；假文档序列化时把它写回文本。

    One element's tag and attributes; the fake document serialises it back to text.
    """

    def __init__(self, tag: str, attributes: dict[str, str] | None = None) -> None:
        self.tag = tag
        self.attributes = dict(attributes or {})
        self.children: list[FakeElement] = []
        self.parent: FakeElement | None = None

    def attribute(self, name: str) -> str:
        return self.attributes.get(name, "")

    def appendChild(self, child: FakeElement) -> FakeElement:
        child.parent = self
        self.children.append(child)
        return child

    def removeChild(self, child: FakeElement) -> FakeElement:
        if child in self.children:
            self.children.remove(child)
            child.parent = None
            return child
        return FakeElement("")

    def parentNode(self) -> FakeElement:
        return self.parent if self.parent is not None else FakeElement("")

    def isNull(self) -> bool:
        return self.tag == ""

    def firstChildElement(self, tag: str) -> FakeElement:
        for child in self.children:
            if child.tag == tag:
                return child
        return FakeElement("")

    def text(self) -> str:
        attributes = "".join(f' {name}="{value}"' for name, value in self.attributes.items())
        if not self.children:
            return f"<{self.tag}{attributes}/>"
        inner = "".join(child.text() for child in self.children)
        return f"<{self.tag}{attributes}>{inner}</{self.tag}>"


OPENING_TAG = re.compile(r"<([A-Za-z0-9_-]+)((?:\s+[A-Za-z0-9_:-]+=\"[^\"]*\")*)\s*/?>")
ATTRIBUTE = re.compile(r"([A-Za-z0-9_:-]+)=\"([^\"]*)\"")


class FakeDocument:
    """``QDomDocument`` 的替身：装一份文本，或者装一棵能序列化的元素树。

    Stand-in for ``QDomDocument``: it holds a text, or an element tree it can serialise.
    """

    def __init__(self) -> None:
        self.content = ""
        self.root: FakeElement | None = None

    def setContent(self, text: str) -> tuple[bool, str, int, int]:
        self.content = text
        match = OPENING_TAG.search(text)
        if match is None or not text.rstrip().endswith(">"):
            return False, "tag mismatch", 1, 10
        self.root = FakeElement(match.group(1), dict(ATTRIBUTE.findall(match.group(2))))
        return True, "", 1, 1

    def documentElement(self) -> FakeElement | None:
        return self.root

    def removeChild(self, node: FakeElement) -> FakeElement:
        if node is self.root:
            self.root = None
            return node
        return FakeElement("")

    def createElement(self, tag: str) -> FakeElement:
        return FakeElement(tag)

    def appendChild(self, node: FakeElement) -> FakeElement:
        self.root = node
        return node

    # Qt 的写法就是 ``toString(2)``；这里照它的签名，分析器把它当成 JS 的同名方法。
    # Qt spells this ``toString(2)``; the analyzer mistakes the name for the JS one.
    def toString(self, indent: int = 0) -> str:
        if self.root is None:
            return self.content
        return self.content if self.content else self.root.text()


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
        # 样式读侧：一个渲染器和一次原生导出。
        # The style read side: a renderer and a native export.
        self.renderer_value: FakeRenderer | None = None
        self.style_xml = "<qgis/>"
        self.style_error = ""
        self.style_exports: list[tuple[Any, Any]] = []
        # 写侧：装上了什么与调用记录。
        # The setter side: what was installed and what was called.
        self.renderer_sets: list[Any] = []
        self.repaints = 0
        self.events: list[str] = []
        self.data_provider: Any = None
        # 栅格类型：默认普通栅格（GrayOrUndefined=0）。
        # The raster type: an ordinary raster by default (GrayOrUndefined = 0).
        self.raster_type = 0
        # 空间与否：属性表不是空间图层，图例给它不给勾选框。
        # Spatial or not: an attribute table is not spatial and gets no legend checkbox.
        self.spatial = True

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

    def isSpatial(self) -> bool:
        return self.spatial

    def rasterType(self) -> int:
        return self.raster_type

    def renderer(self) -> FakeRenderer | None:
        return self.renderer_value

    def exportNamedStyle(self, document: FakeDocument, context: Any, categories: Any) -> str:
        self.style_exports.append((context, categories))
        document.setContent(self.style_xml)
        return self.style_error

    def dataProvider(self) -> Any:
        return self.data_provider

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
        self.nodes: list[Any] = []

    def children(self) -> list[Any]:
        return list(self.nodes)

    def findLayer(self, layer_id: str) -> Any:
        for node in self.nodes:
            if isinstance(node, FakeGroup):
                found = node.findLayer(layer_id)
                if found is not None:
                    return found
            elif node.layerId() == layer_id:
                return node
        return None

    def itemVisibilityChecked(self) -> bool:
        return self.checked

    def isVisible(self) -> bool:
        return self.checked and (self.parent is None or self.parent.isVisible())

    def setItemVisibilityChecked(self, visible: bool) -> None:
        # 原生对同值早返回，所以不记录一次空写。
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

    def __init__(
        self,
        parent: FakeGroup | None = None,
        checked: bool = True,
        layer_id: str = "",
        layer: Any = None,
    ) -> None:
        self.checked = checked
        self.parent = parent
        self.sets: list[bool] = []
        self.layer_id = layer_id
        self.layer_value = layer

    def layerId(self) -> str:
        return self.layer_id

    def layer(self) -> Any:
        return self.layer_value

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
        self.groups: list[FakeGroup] = []

    def findLayer(self, layer_id: str) -> FakeNode | None:
        if layer_id in self.nodes:
            return self.nodes[layer_id]
        for group in self.groups:
            found = group.findLayer(layer_id)
            if found is not None:
                return found
        return None

    def children(self) -> list[Any]:
        # 根的子节点就是分组加上直接挂在根上的图层节点；字典保持插入顺序。
        # The root's children are the groups plus the layer nodes hanging off it; the dict
        # keeps insertion order.
        return [*self.groups, *self.nodes.values()]


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
            self.root.nodes[layer.id()] = FakeNode(layer_id=layer.id(), layer=layer)
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
        # 子图层清单是不是“不完整”（需要更深的扫描），以及栅格路径像不像栅格。
        # Whether the sublayer details are incomplete (a deeper scan is needed), and whether the
        # raster path looks like a raster at all.
        self.incomplete_details = False
        self.raster_file_valid = True
        holder = self

        # 活的注册表：渲染器名字要从这里读，不能猜。默认就装上 QGIS 自带的那几个，和真实注册表
        # 一样；要别的顺序的测试自己改。
        # The live registries.  The renderer names are read, never guessed, and the defaults are the
        # ones QGIS ships; a test that wants another order sets its own.
        self.vector_renderer_types: list[str] = [
            "nullSymbol",
            "singleSymbol",
            "categorizedSymbol",
            "graduatedSymbol",
            "RuleRenderer",
            "heatmapRenderer",
        ]
        self.raster_renderer_types: list[str] = [
            "multibandcolor",
            "paletted",
            "singlebandgray",
            "singlebandpseudocolor",
            "singlebandcolordata",
            "singlecolor",
            "hillshade",
            "contour",
        ]
        self.renderer_registry_layers: list[Any] = []
        self.raster_registries_built = 0

        # 渲染器 setter：造了什么、什么顺序、什么会失败。
        # The renderer setter.
        # What was built, in what order, what fails.
        self.events: list[str] = []
        self.built: list[Any] = []
        self.loaded_documents: list[Any] = []

        class QgsMapLayer:
            """一份样式分类的位标记：每项一位，另有原生那个“全部”。

            A style-category bit flag: one bit each, plus the native "all of them".
            """

            AllStyleCategories = (1 << len(VECTOR_CATEGORIES)) - 1

            @staticmethod
            def StyleCategories() -> int:
                # 原生那个“一个都不选”的位标志（实测 ``QgsMapLayer.StyleCategories()`` 为 0）。
                # The native "nothing selected" flag (measured: it is 0).
                return 0

        for position, category in enumerate(VECTOR_CATEGORIES):
            setattr(QgsMapLayer, category, 1 << position)

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
            DataType=types.SimpleNamespace(Byte=1, Float32=6),
            LayerType=types.SimpleNamespace(Vector=0, Raster=1),
            RasterLayerType=types.SimpleNamespace(
                GrayOrUndefined=0, Palette=1, MultiBand=2, SingleBandColorData=3
            ),
            SublayerQueryFlag=types.SimpleNamespace(IncludeSystemTables=1),
        )

        class FakeRasterProvider:
            """一个栅格提供者：渲染器重建时只把它原样传下去。

            A raster provider: the renderer rebuild only passes it through.
            """

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

            @staticmethod
            def isValidRasterFileName(fileName: str) -> bool:
                # 栅格页签在问 provider 之前先问这一句。
                # The raster tab asks this before it asks any provider.
                return holder.raster_file_valid

        # --- 渲染器文档 / renderer documents ---------------------------------

        def raster_renderer_class(renderer_type: str) -> Any:
            """一个栅格渲染器类：它的 ``create`` 就是那门类型重建设置的入口。

            One raster renderer class: its ``create`` is that type's rebuild entry point.
            """

            class RasterRenderer(FakeRenderer):
                def __init__(self, provider: Any, *arguments: Any) -> None:
                    super().__init__(renderer_type)
                    self.provider = provider
                    self.arguments = arguments
                    # ``create`` 把喂给它的元素记在这里，用例查的就是它。
                    # ``create`` records the element it was handed here; that is what the tests
                    # look at.
                    self.read: Any = None
                    holder.built.append(self)

                @staticmethod
                def create(element: Any, provider: Any) -> Any:
                    holder.loaded_documents.append(element)
                    built = RasterRenderer(provider, element.attribute("type"))
                    built.read = element
                    return built

            return RasterRenderer

        raster_classes = {
            name: raster_renderer_class(name)
            for name in (
                "multibandcolor",
                "paletted",
                "singlebandgray",
                "singlebandpseudocolor",
                "singlebandcolordata",
                "singlecolor",
                "hillshade",
                "contour",
            )
        }

        class QgsFeatureRenderer:
            @staticmethod
            def load(element: Any, context: Any) -> Any:
                holder.loaded_documents.append(element)
                kind = element.attribute("type")
                return FakeRenderer(kind) if kind else None

        class QgsProviderUtils:
            class SublayerCompletenessFlag:
                IgnoreUnknownFeatureCount = 1

            @staticmethod
            def sublayerDetailsAreIncomplete(details: list[Any], flags: int) -> bool:
                return holder.incomplete_details

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

            def querySublayers(self, uri: str, flags: int = 0) -> list[Any]:
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
        # 渲染器文档用到的原生类，供测试里的 isinstance 断言用。
        # The native classes the renderer documents use, for the tests' isinstance assertions.
        self.QgsFeatureRenderer: Any = QgsFeatureRenderer
        self.QgsProviderUtils: Any = QgsProviderUtils
        self.raster_classes: Any = raster_classes

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
        QgsFeatureRenderer=fake.QgsFeatureRenderer,
        QgsProviderUtils=fake.QgsProviderUtils,
        QgsLayerTreeLayer=FakeNode,
        QgsLayerTreeGroup=FakeGroup,
        **{
            RASTER_RENDERER_CLASS_NAMES[name]: renderer_class
            for name, renderer_class in fake.raster_classes.items()
        },
    )
    qtxml = fake_module("qgis.PyQt.QtXml", QDomDocument=FakeDocument)
    pyqt = fake_module("qgis.PyQt", QtXml=qtxml)
    qgis_module = fake_module("qgis", core=core, PyQt=pyqt)
    monkeypatch.setitem(sys.modules, "qgis", qgis_module)
    monkeypatch.setitem(sys.modules, "qgis.core", core)
    monkeypatch.setitem(sys.modules, "qgis.PyQt", pyqt)
    monkeypatch.setitem(sys.modules, "qgis.PyQt.QtXml", qtxml)
    return fake


# --- 辅助函数 / helpers ------------------------------------------------------


def fake_module(name: str, **attributes: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    vars(module).update(attributes)
    return module


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    # 根目录里带个空格。
    # 真实的 ogr URI 必须能扛住它。
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
    # 图层名没有长度上限（实测：200 字的名字原样保住），所以插件也不设。
    # A layer name has no length limit (measured: a 200-character name is kept as is), so the
    # plugin sets none either.
    params = ops.LayerAddParams.model_validate(add_params(name="x" * length))
    assert len(params.name) == length


@pytest.mark.parametrize(
    "sub_layer", ["roads", "roads_1-A", "路段 2024", "roads/1", "roads 1", "x" * 200]
)
def test_any_sub_layer_name_the_provider_can_report_is_accepted(sub_layer: str) -> None:
    # 子图层的名字是 provider 的自由文本；URI 取自它自己那条清单的 uri()（A5）。
    # A sub-layer name is the provider's free text; the URI comes from that provider's own
    # detail (A5).
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
    """provider 的清单为空才是拒的理由，消息是加图层对话框那句。

    An empty provider list is the refusal, with the add-layer dialog's own sentence.
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
    """provider 认得、图层读不出：照样加进图例，靠坏图层图标提示。

    Recognised by the provider but unreadable: it still enters the legend, and the bad-layer
    icon is left to say so.
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
    """选择框只列得出来的东西，所以点不出一个不存在的名字。

    The picker only lists what exists, so a name it never offered is not an answer.
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


def test_a_source_with_one_sublayer_takes_that_sublayers_uri(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """清单里正好一条时用它自己的 URI，而不是把容器路径交给构造器。

    With exactly one entry in the list, its own URI is used rather than handing the container
    path to the constructor.
    """

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
    assert built.source_uri == f"{source.resolve()}|layername=roads"
    assert built.provider == "ogr"
    assert fake_qgis.encoded == []
    assert project.layers[result.layer_id] is built
    assert project.added == [(built, True)]


def test_a_source_with_several_sublayers_is_refused_with_their_names(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """好几个子图层而调用方没点名时，名字要回到调用方，不能静默挑一个。

    With several sublayers and no name from the caller, the names go back rather than one
    being picked silently.
    """

    fake_qgis.sublayer_names = ["roads", "parcels"]
    project = FakeProject()
    source = make_source(data_root)

    with pytest.raises(FrameworkError) as caught:
        ops.LayerAdd().execute_main(
            make_context(project),
            ops.LayerAddParams(**add_params(source_path=str(source))),
        )
    assert caught.value.code == INVALID_LAYER
    assert "parcels" in caught.value.message
    assert "roads" in caught.value.message
    assert project.added == []


def test_incomplete_details_are_refused_even_when_there_is_only_one(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """清单不完整时同样不能挑：那需要一次更深的扫描。

    An incomplete list is not a single entry either: it needs a deeper scan.
    """

    fake_qgis.sublayer_names = ["roads"]
    fake_qgis.incomplete_details = True
    project = FakeProject()

    with pytest.raises(FrameworkError) as caught:
        ops.LayerAdd().execute_main(
            make_context(project),
            ops.LayerAddParams(**add_params(source_path=str(make_source(data_root)))),
        )
    assert caught.value.code == INVALID_LAYER
    assert "roads" in caught.value.message


def test_a_provider_that_cannot_list_sublayers_takes_the_path_itself(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """不会列子图层的 provider 没有清单可挑，所以路径本身就是那个源。

    A provider that cannot list sublayers has no list to pick from, so the path is the source.
    """

    fake_qgis.query_sublayers_capability = 0
    project = FakeProject()
    source = make_source(data_root)

    result = ops.LayerAdd().execute_main(
        make_context(project),
        ops.LayerAddParams(**add_params(source_path=str(source))),
    )
    assert fake_qgis.constructed[0].source_uri == str(source.resolve())
    assert result.provider == "ogr"

    # 那条分支不提供子数据集选择，所以点名一个子图层在那里无法达成。
    # That branch offers no subdataset choice, so naming a sublayer there cannot be satisfied.
    with pytest.raises(FrameworkError) as caught:
        ops.LayerAdd().execute_main(
            make_context(FakeProject()),
            ops.LayerAddParams(**add_params(source_path=str(source), sub_layer="roads")),
        )
    assert caught.value.code == INVALID_LAYER
    assert caught.value.message == f"{source.resolve()} does not offer sub-layers"


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
    # URI 就是那条 detail 的 uri()——图层工厂拿到的同一个字符串，不再自己调 encodeUri 拼。
    # The URI is that detail's own uri() -- the same string the layer factory is handed; nothing
    # re-encodes it here.
    assert fake_qgis.provider_names == ["ogr"]
    assert fake_qgis.encoded == []
    assert fake_qgis.constructed[0].source_uri == f"{resolved}|layername=roads"
    assert result.provider == "ogr"


def test_a_raster_can_carry_a_sub_layer(fake_qgis: FakeQgis, data_root: Path) -> None:
    """栅格也有子数据集清单：那个页签同样提供它。

    A raster has a subdataset list too: that tab offers one as well.
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


def test_a_file_that_is_not_a_raster_is_refused_before_any_provider(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """栅格页签先问文件像不像栅格，不像就一个都不加。

    The raster tab first asks whether the file even looks like a raster, and adds nothing
    when it does not.
    """

    fake_qgis.raster_file_valid = False
    project = FakeProject()
    source = make_source(data_root, "notes.txt")

    with pytest.raises(FrameworkError) as caught:
        ops.LayerAdd().execute_main(
            make_context(project),
            ops.LayerAddParams(**add_params(kind="raster", source_path=str(source))),
        )
    assert caught.value.code == INVALID_LAYER
    assert caught.value.message == f"{source.resolve()} is not a supported raster data source"
    assert fake_qgis.sublayer_queries == []
    assert project.added == []


def test_a_legend_entry_is_always_created(fake_qgis: FakeQgis, data_root: Path) -> None:
    """没有 `add_to_legend` 参数：每次加图层都进图例。

    另一个入口的 addToLegend=false 是“先进工程、随后进分组”，图层仍在图例里。
    There is no `add_to_legend` parameter: every add enters the legend.  The other entry point's
    addToLegend=false means "into the project, then into a group", and the layer stays in the
    legend all the same.
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


def test_layers_are_listed_in_legend_order(fake_qgis: FakeQgis, data_root: Path) -> None:
    """图例是图层树的顺序，不是 ``mapLayers()`` 的 id 顺序。

    The legend follows the layer tree, not the id order ``mapLayers()`` hands out.
    """

    project = FakeProject()
    for layer_id in ("zzz", "aaa", "mmm"):
        layer = make_layer(fake_qgis, layer_id)
        project.layers[layer_id] = layer
        project.root.nodes[layer_id] = FakeNode(layer_id=layer_id, layer=layer)

    result = ops.LayerList().execute_main(make_context(project), ops.LayerListParams())
    assert [summary.layer_id for summary in result.layers] == ["zzz", "aaa", "mmm"]


def test_layers_without_a_node_come_after_the_legend(fake_qgis: FakeQgis, data_root: Path) -> None:
    """没有节点的图层不在图例里，所以排在后面，而不是从列表里消失。

    A layer without a node is not in the legend, so it comes last rather than vanishing.
    """

    project = FakeProject()
    untreed = make_layer(fake_qgis, "untreed")
    project.layers[untreed.id()] = untreed
    for layer_id in ("zzz", "aaa"):
        layer = make_layer(fake_qgis, layer_id)
        project.layers[layer_id] = layer
        project.root.nodes[layer_id] = FakeNode(layer_id=layer_id, layer=layer)

    result = ops.LayerList().execute_main(make_context(project), ops.LayerListParams())
    assert [summary.layer_id for summary in result.layers] == ["zzz", "aaa", "untreed"]


def test_the_order_walks_into_groups(fake_qgis: FakeQgis, data_root: Path) -> None:
    """分组也是图例顺序的一部分：先外层分组自己，再它的子树。

    A group is part of the legend order too: the outer group first, then its subtree.
    """

    project = FakeProject()
    outer, inner = FakeGroup(), FakeGroup()
    nodes = {}
    for layer_id in ("outer-a", "inner-a", "inner-b"):
        layer = make_layer(fake_qgis, layer_id)
        project.layers[layer_id] = layer
        nodes[layer_id] = FakeNode(layer_id=layer_id, layer=layer)

    project.root.groups.append(outer)
    outer.nodes = [nodes["outer-a"], inner]
    inner.nodes = [nodes["inner-a"], nodes["inner-b"]]

    result = ops.LayerList().execute_main(make_context(project), ops.LayerListParams())
    assert [summary.layer_id for summary in result.layers] == [
        "outer-a",
        "inner-a",
        "inner-b",
    ]
    # 分组里的图层照样算在树里，只是勾选状态由节点决定。
    # A grouped layer is still in the tree; only its check state comes from its node.
    assert all(summary.in_layer_tree for summary in result.layers)


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


def test_setting_the_same_state_writes_nothing_and_marks_nothing_dirty(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """写入口对同值是空操作，所以同值重写也不该把工程置脏。

    The setters are no-ops on an unchanged value, so an unchanged rewrite does not mark the
    project dirty either.
    """

    project = FakeProject()
    layer = make_layer(fake_qgis, "roads")
    project.layers[layer.id()] = layer
    node = FakeNode(layer_id=layer.id(), layer=layer)
    project.root.nodes[layer.id()] = node

    result = ops.LayerSetVisibility().execute_main(
        make_context(project),
        ops.LayerSetVisibilityParams(layer_id=layer.id(), visible=True),
    )
    assert node.sets == []
    assert project.dirty == []
    assert (result.visible, result.effective_visible) == (True, True)
    assert set(result.model_dump()) == {"layer_id", "visible", "effective_visible"}


def test_unchecking_touches_only_that_node(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    layer = make_layer(fake_qgis, "roads")
    project.layers[layer.id()] = layer
    group = FakeGroup(checked=True)
    node = FakeNode(parent=group, layer_id=layer.id(), layer=layer)
    project.root.nodes[layer.id()] = node

    result = ops.LayerSetVisibility().execute_main(
        make_context(project),
        ops.LayerSetVisibilityParams(layer_id=layer.id(), visible=False),
    )
    assert node.sets == [False]
    assert group.sets == []
    assert project.dirty == [True]
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
    node = FakeNode(parent=group, layer_id=layer.id(), layer=layer)
    project.root.nodes[layer.id()] = node

    result = ops.LayerSetVisibility().execute_main(
        make_context(project),
        ops.LayerSetVisibilityParams(layer_id=layer.id(), visible=True),
    )
    # 节点本来就勾着，所以它自己那次写是空写（原生同值早返回）；真正变的是两个分组。
    # The node was already ticked, so its own write is a no-op (native early return);
    # what changes are the two groups.
    assert node.sets == []
    assert group.sets == [True]
    assert outer.sets == [True]
    assert project.dirty == [True]
    assert (result.visible, result.effective_visible) == (True, True)


def test_a_type_outside_the_combo_is_refused_before_anything_is_built(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """类型名在解析完就在文档根元素上，所以不在清单里时连 ``load`` 都不该进。

    The type name is on the root element as soon as the document parses, so a type outside the list
    must not reach ``load`` at all.
    """

    project = FakeProject()
    layer = style_layer(fake_qgis, project, "roads")

    code = refuse(
        ops.LayerSetRenderer(),
        make_context(project),
        ops.LayerSetRendererParams(layer_id="roads", renderer='<renderer-v2 type="p5NotOffered"/>'),
    )
    assert code == INVALID_INPUT
    assert fake_qgis.loaded_documents == []
    assert layer.renderer_sets == []


def test_a_non_spatial_layer_has_no_visibility_to_set(fake_qgis: FakeQgis, data_root: Path) -> None:
    """属性表在图例里没有勾选框，所以既读不到也写不了。

    An attribute table has no legend checkbox, so it can neither be read nor written.
    """

    project = FakeProject()
    layer = make_layer(fake_qgis, "table")
    layer.spatial = False
    project.layers[layer.id()] = layer
    project.root.nodes[layer.id()] = FakeNode(layer_id=layer.id(), layer=layer)
    ctx = make_context(project)

    info = ops.LayerGetInfo().execute_main(ctx, ops.LayerGetInfoParams(layer_id="table"))
    assert (info.in_layer_tree, info.visible, info.effective_visible) == (True, None, None)
    code = refuse(
        ops.LayerSetVisibility(),
        ctx,
        ops.LayerSetVisibilityParams(layer_id="table", visible=False),
    )
    assert code == INVALID_LAYER
    assert project.dirty == []


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
    renderer: str | None = "singleSymbol",
) -> Any:
    layer = make_layer(fake, layer_id, name=name, kind=kind)
    # 一个空间图层总是带着渲染器；没有几何的图层没有。
    # A spatial layer always carries a renderer; a geometry-less one does not.
    layer.renderer_value = FakeRenderer(renderer) if renderer is not None else None
    project.layers[layer.id()] = layer
    project.root.nodes[layer.id()] = FakeNode(layer_id=layer.id(), layer=layer)
    return layer


def style_request(operation: Any, layer_id: str, **overrides: Any) -> Any:
    return operation.input_model.model_validate({"layer_id": layer_id, **overrides})


def category_flag(fake: FakeQgis, names: Any) -> int:
    flag = 0
    for name in names:
        flag |= getattr(fake.QgsMapLayer, name)
    return flag


def renderer_document(kind: str) -> str:
    """一份最小渲染器文档：根元素的名字无所谓，``type`` 属性才是渲染器类型。

    A minimal renderer document: the root's name does not matter, the ``type`` attribute
    is the renderer type.
    """

    element = "rasterrenderer" if kind == "raster" else "renderer-v2"
    return f'<{element} type="singleSymbol"/>'


def test_a_vector_layer_has_the_eighteen_categories_in_order(
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
    # 栅格的勾选框里没有 Labeling。
    # A raster's checkboxes do not include Labeling.
    assert "Labeling" not in result.categories


@pytest.mark.parametrize("operation", STYLE_READS)
def test_every_style_read_refuses_an_unknown_layer(
    fake_qgis: FakeQgis, data_root: Path, operation: Any
) -> None:
    ctx = make_context(FakeProject())
    assert refuse(operation(), ctx, style_request(operation, "missing")) == LAYER_NOT_FOUND


@pytest.mark.parametrize("operation", STYLE_READS)
def test_every_style_read_refuses_another_layer_kind(
    fake_qgis: FakeQgis, data_root: Path, operation: Any
) -> None:
    project = FakeProject()
    project.layers["plugin-1"] = FakeLayer("plugin-1", "annotations", provider="custom", authid="")

    ctx = make_context(project)
    request = {"renderer": renderer_document("vector")} if operation is ops.LayerSetRenderer else {}
    assert (
        refuse(operation(), ctx, style_request(operation, "plugin-1", **request)) == INVALID_LAYER
    )


def test_a_geometry_less_vector_table_keeps_its_style_reads(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """属性表也有字段、表单、属性表设置这些分类，也能导出 QML。

    A table still has fields, forms and attribute-table settings to export.
    """

    project = FakeProject()
    layer = style_layer(fake_qgis, project, "table", renderer=None)
    layer.spatial = False
    ctx = make_context(project)

    categories = ops.LayerListStyleCategories().execute_main(
        ctx, ops.LayerListStyleCategoriesParams(layer_id="table")
    )
    assert tuple(categories.categories) == VECTOR_CATEGORIES

    qml = ops.LayerGetQml().execute_main(ctx, ops.LayerGetQmlParams(layer_id="table"))
    assert qml.qml == layer.style_xml


def test_get_qml_without_categories_exports_every_category(
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
    assert flag == fake_qgis.QgsMapLayer.AllStyleCategories


def test_get_qml_asks_for_the_same_every_category_on_a_raster(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """没点名分类就是全部位；导出只写这门图层类型有的那几类。

    Naming nothing means every bit; the export only writes the ones this layer type has.
    """

    project = FakeProject()
    layer = style_layer(fake_qgis, project, "dem", kind="raster")

    ops.LayerGetQml().execute_main(make_context(project), ops.LayerGetQmlParams(layer_id="dem"))
    assert layer.style_exports[0][1] == fake_qgis.QgsMapLayer.AllStyleCategories


def test_get_qml_ors_the_requested_categories(fake_qgis: FakeQgis, data_root: Path) -> None:
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


def test_get_qml_collapses_a_repeated_category(fake_qgis: FakeQgis, data_root: Path) -> None:
    """分类是一个位标记，所以重复的名字就是一个位。

    The categories are one bit flag, so a repeated name is one bit.
    """

    project = FakeProject()
    layer = style_layer(fake_qgis, project, "roads")

    ops.LayerGetQml().execute_main(
        make_context(project),
        ops.LayerGetQmlParams(layer_id="roads", categories=["Symbology", "Symbology"]),
    )
    assert layer.style_exports[0][1] == category_flag(fake_qgis, ("Symbology",))


def test_get_qml_takes_a_category_the_layer_type_does_not_use(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """不适用的分类只是一个位；导出自己决定写不写，不动就是零字节。

    A category the layer type does not use is just a bit; the export decides whether to
    write it, and writes nothing when it does not.
    """

    project = FakeProject()
    layer = style_layer(fake_qgis, project, "dem", kind="raster")

    ops.LayerGetQml().execute_main(
        make_context(project),
        ops.LayerGetQmlParams(layer_id="dem", categories=["Symbology3D"]),
    )
    assert layer.style_exports[0][1] == category_flag(fake_qgis, ("Symbology3D",))


def test_get_qml_with_an_empty_category_list_exports_the_empty_set(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """ "一个都不选"是 Deselect All + OK，不是错误。

    "None of them" is Deselect All plus OK, not an error.
    """

    assert ops.LayerGetQmlParams(layer_id="roads").categories is None
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "roads")

    result = ops.LayerGetQml().execute_main(
        make_context(project),
        ops.LayerGetQmlParams.model_validate({"layer_id": "roads", "categories": []}),
    )
    assert result.qml == layer.style_xml
    # 空标志就是原生那个"零分类"。
    # The empty flag is the native "zero categories".
    assert layer.style_exports[0][1] == 0
    assert category_flag(fake_qgis, ()) == 0


def test_an_unknown_category_name_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError):
        ops.LayerGetQmlParams.model_validate({"layer_id": "roads", "categories": ["Avaiable"]})


def test_a_failed_export_delivers_the_native_message(fake_qgis: FakeQgis, data_root: Path) -> None:
    # 3.44 用非空消息表示导出失败，而不是 (message, ok) 这一对；那句话就是交给调用方的那句。
    # A failed export is a non-empty message, not a (message, ok) pair; that sentence is what
    # the caller gets.
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "roads")
    layer.style_error = "Could not save symbology because:\nno renderer"

    with pytest.raises(FrameworkError) as caught:
        ops.LayerGetQml().execute_main(
            make_context(project), ops.LayerGetQmlParams(layer_id="roads")
        )
    assert caught.value.code == INTERNAL_ERROR
    assert caught.value.message == layer.style_error


def test_the_vector_renderer_list_is_the_registry_order(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    fake_qgis.vector_renderer_types = ["singleSymbol", "RuleRenderer", "heatmapRenderer"]
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "roads")

    result = ops.LayerListAvailableRenderers().execute_main(
        make_context(project),
        ops.LayerListAvailableRenderersParams(layer_id="roads"),
    )
    assert result.renderer_types == ["singleSymbol", "RuleRenderer", "heatmapRenderer"]
    assert set(result.model_dump()) == {"renderer_types"}
    assert fake_qgis.renderer_registry_layers == [layer]
    assert fake_qgis.raster_registries_built == 0


def test_the_raster_renderer_list_is_the_registry_filtered_by_the_raster_type(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """普通栅格的下拉框不列 ``singlebandcolordata``，顺序照注册表。

    An ordinary raster's combo does not list ``singlebandcolordata``, in registry order.
    """

    fake_qgis.raster_renderer_types = [
        "singlecolor",
        "multibandcolor",
        "singlebandcolordata",
    ]
    project = FakeProject()
    style_layer(fake_qgis, project, "dem", kind="raster")

    result = ops.LayerListAvailableRenderers().execute_main(
        make_context(project),
        ops.LayerListAvailableRenderersParams(layer_id="dem"),
    )
    assert result.renderer_types == ["singlecolor", "multibandcolor"]
    assert fake_qgis.renderer_registry_layers == []
    assert fake_qgis.raster_registries_built == 1


def test_a_single_band_color_data_raster_lists_only_that_renderer(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """``SingleBandColorData`` 栅格的下拉框里只有那一个（枚举值 3）。

    A ``SingleBandColorData`` raster's combo holds only that one (enum value 3).
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


@pytest.mark.parametrize(
    "operation",
    (ops.LayerListAvailableRenderers, ops.LayerGetRenderer, ops.LayerSetRenderer),
)
def test_every_renderer_operation_refuses_a_layer_without_a_renderer(
    fake_qgis: FakeQgis, data_root: Path, operation: Any
) -> None:
    """没有渲染器的图层没有符号页，所以它也换不了渲染器。

    A layer without a renderer has no symbology page, so it cannot take one either.
    """

    project = FakeProject()
    layer = style_layer(fake_qgis, project, "table", renderer=None)
    layer.spatial = False

    request = {"renderer": renderer_document("vector")} if operation is ops.LayerSetRenderer else {}
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
        (
            ops.LayerGetRendererResult,
            {"layer_id": "roads", "renderer_type": "", "renderer_xml": "", "config": {}},
        ),
        (ops.LayerSetRendererResult, {"layer_id": "roads", "renderer_type": "", "xml": ""}),
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


# --- 渲染器文档 / the renderer documents ----------------------------------


def renderer_xml(project: FakeProject, layer_id: str) -> str:
    result = ops.LayerGetRenderer().execute_main(
        make_context(project), ops.LayerGetRendererParams(layer_id=layer_id)
    )
    return result.renderer_xml


def test_get_renderer_reports_the_type_and_the_renderers_own_document(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    vector = style_layer(fake_qgis, project, "roads")
    vector.renderer_value = FakeRenderer("categorizedSymbol")
    raster = style_layer(fake_qgis, project, "dem", kind="raster")
    raster.renderer_value = FakeRenderer("singlebandgray")
    ctx = make_context(project)

    vector_result = ops.LayerGetRenderer().execute_main(
        ctx, ops.LayerGetRendererParams(layer_id="roads")
    )
    raster_result = ops.LayerGetRenderer().execute_main(
        ctx, ops.LayerGetRendererParams(layer_id="dem")
    )

    assert vector_result.renderer_type == "categorizedSymbol"
    assert vector_result.renderer_xml == '<renderer-v2 type="categorizedSymbol"/>'
    assert set(vector_result.model_dump()) == {"layer_id", "renderer_type", "renderer_xml"}
    # 栅格渲染器写在容器里，交出来的是它自己那个元素。
    # A raster renderer writes into a container; the element it hands back is its own.
    assert raster_result.renderer_type == "singlebandgray"
    assert raster_result.renderer_xml == '<rasterrenderer type="singlebandgray"/>'


def test_a_layer_beyond_the_plain_geometry_types_still_answers_the_renderer_reads(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """一个渲染器类型不在任何几何白名单里时，两个读照答：清单与当前类型都从图层自己来。

    When the renderer type is on no geometry whitelist of ours, both reads still answer: the list
    and the current type both come from the layer itself.
    """

    fake_qgis.vector_renderer_types = ["heatmapRenderer", "singleSymbol"]
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "odd", renderer="heatmapRenderer")
    ctx = make_context(project)

    renderer = ops.LayerGetRenderer().execute_main(ctx, ops.LayerGetRendererParams(layer_id="odd"))
    assert renderer.renderer_type == "heatmapRenderer"

    listed = ops.LayerListAvailableRenderers().execute_main(
        ctx, ops.LayerListAvailableRenderersParams(layer_id="odd")
    )
    assert listed.renderer_types == ["heatmapRenderer", "singleSymbol"]
    assert fake_qgis.renderer_registry_layers == [layer]


def test_set_renderer_installs_what_the_document_names(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "roads", renderer="singleSymbol")
    ctx = make_context(project)

    result = ops.LayerSetRenderer().execute_main(
        ctx,
        ops.LayerSetRendererParams(layer_id="roads", renderer=renderer_document("vector")),
    )

    installed = layer.renderer_sets[0]
    assert isinstance(installed, FakeRenderer)
    assert installed.type() == "singleSymbol"
    assert result.renderer_type == "singleSymbol"
    assert layer.events == ["setRenderer", "repaint"]
    assert project.dirty == [True]
    assert set(result.model_dump()) == {"layer_id", "renderer_type"}


def test_set_renderer_hands_the_document_element_to_the_native_reader(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """矢量渲染器由 ``QgsFeatureRenderer.load`` 重建，就是读样式文件时那一次调用。

    A vector renderer is rebuilt by ``QgsFeatureRenderer.load``, the call a style file goes
    through.
    """

    project = FakeProject()
    style_layer(fake_qgis, project, "roads")
    document = '<renderer-v2 type="RuleRenderer"><rules/></renderer-v2>'

    ops.LayerSetRenderer().execute_main(
        make_context(project),
        ops.LayerSetRendererParams(layer_id="roads", renderer=document),
    )

    element = fake_qgis.loaded_documents[0]
    assert element.tag == "renderer-v2"
    assert element.attribute("type") == "RuleRenderer"
    assert fake_qgis.built == []


@pytest.mark.parametrize(
    "renderer_type",
    sorted(
        (
            "multibandcolor",
            "paletted",
            "singlebandgray",
            "singlebandpseudocolor",
            "singlecolor",
            "hillshade",
            "contour",
        )
    ),
)
def test_a_raster_document_installs_every_registry_type(
    fake_qgis: FakeQgis, data_root: Path, renderer_type: str
) -> None:
    """下拉框里的类型都能从文档装回去，由那门类型自己的 ``create`` 重建。

    Every type the combo lists installs from a document, rebuilt by that type's own ``create``.
    """

    project = FakeProject()
    layer = style_layer(fake_qgis, project, "dem", kind="raster")
    document = f'<rasterrenderer type="{renderer_type}"/>'

    result = ops.LayerSetRenderer().execute_main(
        make_context(project),
        ops.LayerSetRendererParams(layer_id="dem", renderer=document),
    )

    installed = layer.renderer_sets[0]
    assert isinstance(installed, fake_qgis.raster_classes[renderer_type])
    assert installed.type() == renderer_type
    assert installed.read is not None
    assert installed.read.attribute("type") == renderer_type
    assert result.renderer_type == renderer_type


def test_a_raster_type_the_combo_does_not_list_is_refused(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """普通栅格的下拉框里没有 ``singlebandcolordata``，而且 QGIS 自己也把它默不作声地丢掉。

    An ordinary raster's combo has no ``singlebandcolordata``, and QGIS itself drops it without a
    word.
    """

    project = FakeProject()
    layer = style_layer(fake_qgis, project, "dem", kind="raster")

    code = refuse(
        ops.LayerSetRenderer(),
        make_context(project),
        ops.LayerSetRendererParams(
            layer_id="dem", renderer='<rasterrenderer type="singlebandcolordata"/>'
        ),
    )
    assert code == INVALID_INPUT
    assert layer.renderer_sets == []


def test_a_single_band_color_data_raster_takes_that_renderer(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """那门类型的栅格只有这一个候选，所以它也装得上。

    A raster of that type has only that one candidate, so it installs.
    """

    project = FakeProject()
    layer = style_layer(fake_qgis, project, "rgba", kind="raster")
    layer.raster_type = 3  # 对应 Qgis.RasterLayerType.SingleBandColorData

    result = ops.LayerSetRenderer().execute_main(
        make_context(project),
        ops.LayerSetRendererParams(
            layer_id="rgba", renderer='<rasterrenderer type="singlebandcolordata"/>'
        ),
    )
    assert result.renderer_type == "singlebandcolordata"
    assert layer.renderer_sets[0].type() == "singlebandcolordata"


def test_a_raster_document_of_an_unknown_type_installs_nothing(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "dem", kind="raster")

    code = refuse(
        ops.LayerSetRenderer(),
        make_context(project),
        ops.LayerSetRendererParams(
            layer_id="dem", renderer='<rasterrenderer type="notARenderer"/>'
        ),
    )
    assert code == INVALID_INPUT
    assert layer.renderer_sets == []
    assert fake_qgis.built == []


def test_a_document_that_is_not_xml_is_refused_with_the_parsers_position(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = style_layer(fake_qgis, project, "roads")

    with pytest.raises(FrameworkError) as caught:
        ops.LayerSetRenderer().execute_main(
            make_context(project),
            ops.LayerSetRendererParams(layer_id="roads", renderer="not xml at all"),
        )
    assert caught.value.code == INVALID_INPUT
    assert caught.value.message == "tag mismatch at line 1 column 10"
    assert layer.renderer_sets == []


def test_a_document_without_a_renderer_type_is_refused(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """读得出来但没有类型：矢量侧的重建返回空，栅格侧的查表也落空。

    It parses but names no type: the vector reader returns nothing and the raster lookup
    finds no class.
    """

    project = FakeProject()
    vector = style_layer(fake_qgis, project, "roads")
    raster = style_layer(fake_qgis, project, "dem", kind="raster")
    ctx = make_context(project)

    assert (
        refuse(
            ops.LayerSetRenderer(),
            ctx,
            ops.LayerSetRendererParams(layer_id="roads", renderer="<renderer-v2/>"),
        )
        == INVALID_INPUT
    )
    assert (
        refuse(
            ops.LayerSetRenderer(),
            ctx,
            ops.LayerSetRendererParams(layer_id="dem", renderer="<rasterrenderer/>"),
        )
        == INVALID_INPUT
    )
    assert vector.renderer_sets == []
    assert raster.renderer_sets == []
    assert project.dirty == []


def test_the_document_survives_a_round_trip_through_the_setter(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """读出来的文档装回去是同一个类型：读与写是同一件事的两个方向。

    The document handed out installs back as the same type: read and write are one thing
    in two directions.
    """

    project = FakeProject()
    layer = style_layer(fake_qgis, project, "roads", renderer="graduatedSymbol")
    ctx = make_context(project)
    document = renderer_xml(project, "roads")

    result = ops.LayerSetRenderer().execute_main(
        ctx, ops.LayerSetRendererParams(layer_id="roads", renderer=document)
    )
    assert result.renderer_type == "graduatedSymbol"
    assert layer.renderer_sets[0].type() == "graduatedSymbol"


def test_a_renderer_request_needs_a_layer_id_and_a_document() -> None:
    with pytest.raises(ValidationError):
        ops.LayerSetRendererParams.model_validate({"layer_id": "", "renderer": "<x/>"})
    with pytest.raises(ValidationError):
        ops.LayerSetRendererParams.model_validate({"layer_id": "roads", "renderer": ""})
    with pytest.raises(ValidationError):
        ops.LayerSetRendererParams.model_validate(
            {"layer_id": "roads", "renderer": "<x/>", "editable": True}
        )


def test_the_renderer_setter_registers_as_a_destructive_command() -> None:
    registry = OperationRegistry()
    registry.register(ops.LayerSetRenderer())

    (spec,) = registry.list_specs()
    assert (spec.name, spec.kind, spec.strategy, spec.destructive) == (
        "layer.set_renderer",
        "command",
        "main_thread",
        True,
    )
