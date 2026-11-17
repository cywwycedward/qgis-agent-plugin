# 静态分析器自己的解释器里没有 qgis/pydantic，所以本文件只关掉“找不到模块”这一类报告；能不能
# 真的加载由 QGIS 解释器的验收跑证明。
# The analyzer's own interpreter has no qgis/pydantic, so this file turns off only the
# missing-module report; whether it really loads is proven by the QGIS interpreter's run.
# pyright: reportMissingImports=false

"""验收用例：渲染器候选、读写与安装。

注册表能给出什么、一份渲染器文档怎么换到另一个图层、八种栅格渲染器各自的文档，以及读不
懂或装不上的时候被拒在哪。

The acceptance cases for renderers: what the registry offers, how a renderer document moves to
another layer, the document of each of the eight raster renderers, and where an unreadable or
uninstallable one is refused.
"""

from __future__ import annotations

from typing import Any

import fixtures  # pyright: ignore[reportMissingImports]
from harness import Context, Skip, equal, require  # pyright: ignore[reportMissingImports]
from helpers import (
    cities_layer,
    error_code,
    installed_renderer,
    message_of,
    project_layer,
    qml_of,
    raster_layer,
    result_of,
    set_renderer,
    sub_layer,
    xml_root,
)

from qgis_agent_plugin.core.models import (  # pyright: ignore[reportMissingImports]
    INVALID_INPUT,
    INVALID_LAYER,
    LAYER_NOT_FOUND,
    ExecutionStatus,
)

RASTER_RENDERER_TYPES = [
    "contour",
    "hillshade",
    "multibandcolor",
    "paletted",
    "singlebandcolordata",
    "singlebandgray",
    "singlebandpseudocolor",
    "singlecolor",
]


def a01_vector_candidates(ctx: Context) -> str:
    from qgis.core import QgsApplication  # pyright: ignore[reportMissingImports]

    layers = {
        "Point": cities_layer(ctx, "a01 point"),
        "Line": sub_layer(ctx, "a01 line", fixtures.ROADS_LAYER),
        "Polygon": sub_layer(ctx, "a01 polygon", fixtures.PARCELS_LAYER),
    }
    lists = {}
    for geometry, layer_id in layers.items():
        native = QgsApplication.rendererRegistry().renderersList(project_layer(layer_id))
        record = ctx.run("layer.list_available_renderers", {"layer_id": layer_id})
        equal(record.status, ExecutionStatus.SUCCEEDED, f"{geometry}: status")
        lists[geometry] = result_of(record)["renderer_types"]
        equal(lists[geometry], native, f"{geometry}: the live per-layer registry list")
    require("heatmapRenderer" in lists["Point"], f"the Point list: {lists['Point']}")
    require(
        "heatmapRenderer" not in lists["Line"],
        f"the Line list holds a Point-only renderer: {lists['Line']}",
    )
    require(lists["Point"] != lists["Polygon"], "every geometry got the same list")
    return (
        f"each geometry's list equals renderersList(layer) of the live registry "
        f"(Point {len(lists['Point'])}, Line {len(lists['Line'])}, Polygon "
        f"{len(lists['Polygon'])} types), so the registry's own geometry filter is what decides"
    )


def a02_raster_candidates(ctx: Context) -> str:
    from qgis.core import Qgis, QgsRasterRendererRegistry  # pyright: ignore[reportMissingImports]

    native = QgsRasterRendererRegistry().renderersList()
    equal(sorted(native), RASTER_RENDERER_TYPES, "the eight built-in raster renderers")
    equal(sorted(set(native)), sorted(native), "the raster registry repeats itself")

    layers = {
        "one-band": raster_layer(ctx, "a02 one-band", ctx.fixtures.raster),
        "three-band": raster_layer(ctx, "a02 three-band", ctx.fixtures.raster3),
    }
    for label, layer_id in layers.items():
        layer = project_layer(layer_id)
        # 下拉框 = 注册表清单再按 rasterType() 过滤一层。
        # The combo is the registry list filtered once more by rasterType().
        single_band_color_data = layer.rasterType() == Qgis.RasterLayerType.SingleBandColorData
        expected = [
            name for name in native if (name == "singlebandcolordata") == single_band_color_data
        ]
        record = ctx.run("layer.list_available_renderers", {"layer_id": layer_id})
        equal(
            record.status,
            ExecutionStatus.SUCCEEDED,
            f"{label}: layer.list_available_renderers: {record.error}",
        )
        types = result_of(record)["renderer_types"]
        equal(types, expected, f"{label}: the layer's own combo list")
        require(
            "singlebandcolordata" not in types,
            f"{label}: singlebandcolordata is not in an ordinary raster's combo ({types})",
        )
    require(len(native) == 8, f"the registry count changed: {len(native)}")
    return (
        "the registry holds eight raster renderers, while a raster layer's list is the registry "
        "filtered by its own rasterType() as the symbology combo does: seven names each for a "
        "one-band and a three-band raster, with singlebandcolordata left out"
    )


# 由 A03 注册进应用级注册表，所以这个实例要活得比用例久：原生注册表接管所有权，Python 必须留住
# 包装对象。
# Registered into the application registry by A03, so the instance outlives the case: the native
# registry takes ownership, and Python must keep its wrapper alive.
_REGISTERED_RENDERER_METADATA: list[Any] = []


def a03_third_party_candidate(ctx: Context) -> str:
    from qgis.core import (  # pyright: ignore[reportMissingImports]
        QgsApplication,
        QgsRendererAbstractMetadata,
    )

    layer_id = cities_layer(ctx, "a03 point")
    before = result_of(ctx.run("layer.list_available_renderers", {"layer_id": layer_id}))[
        "renderer_types"
    ]
    # 候选清单来自注册表，不按任何本地清单过滤。
    # The candidate list comes from the registry, unfiltered by any table kept here.
    for name in ("heatmapRenderer", "pointCluster", "pointDisplacement"):
        require(name in before, f"{name} is missing from the candidates: {before}")

    class AcceptanceMetadata(QgsRendererAbstractMetadata):
        def __init__(self) -> None:
            super().__init__("p5AcceptanceRenderer", "P5 acceptance renderer")

        def createRenderer(self, element):  # noqa: N802 - the native spelling
            # 不会被调到：让 PyQt 的 C++ 调用进一个 Python 建的元数据会当场结束进程
            # （0xC0000409，sip 所有权，实测）。这里只证明清单是活的。
            # Never called: C++ calling into a Python-built metadata ends the process outright
            # (0xC0000409, sip ownership, measured).  This only proves the list is live.
            raise AssertionError("createRenderer must not be called")

    metadata = AcceptanceMetadata()
    require(
        QgsApplication.rendererRegistry().addRenderer(metadata),
        "the application renderer registry refused the new renderer",
    )
    _REGISTERED_RENDERER_METADATA.append(metadata)

    after = result_of(ctx.run("layer.list_available_renderers", {"layer_id": layer_id}))[
        "renderer_types"
    ]
    require(
        "p5AcceptanceRenderer" in after,
        f"a renderer registered in this session is not a candidate: {after}",
    )
    # 装得上的东西由注册表决定：heatmapRenderer 是注册表里的 C++ 渲染器。
    # What installs is decided by the registry: heatmapRenderer is a C++ one it holds.
    heatmap = ctx.run("layer.get_renderer", {"layer_id": cities_layer(ctx, "a03 heatmap")})
    equal(heatmap.status, ExecutionStatus.SUCCEEDED, "a second point layer")
    installed = set_renderer(ctx, layer_id, '<renderer-v2 type="heatmapRenderer"/>')
    equal(installed.status, ExecutionStatus.SUCCEEDED, f"a registry type: {installed.error}")
    equal(
        result_of(installed)["renderer_type"],
        "heatmapRenderer",
        "the type the registry's own create function built",
    )
    equal(
        project_layer(layer_id).renderer().type(),
        "heatmapRenderer",
        "the renderer on the layer",
    )
    return (
        "the candidates include the three registered Point types, a renderer registered into the "
        "application registry during this run appears in the next call, and a registry type the "
        "old setter could not build (heatmapRenderer) now installs from its document"
    )


def a03b_raster_third_party(ctx: Context) -> str:
    from qgis.core import QgsApplication  # pyright: ignore[reportMissingImports]

    # 这个访问器在 3.44 里没绑定；而这里新建的注册表对象也不是第三方提供者会注册进去的那个 C++
    # 单例。
    # The accessor is not bound in 3.44, and a registry object built here is not the C++ singleton a
    # third-party provider would register into.
    require(
        not hasattr(QgsApplication, "rasterRendererRegistry"),
        "QgsApplication now exposes rasterRendererRegistry(); the A03 raster half can be run",
    )
    raise Skip(
        "QgsApplication.rasterRendererRegistry() is SIP_SKIP in this build, "
        "QgsRasterRendererRegistry has no instance() and a freshly constructed one lists only the "
        "eight built-ins, so a third-party raster renderer registered in the C++ singleton cannot "
        "be observed from Python: the raster half of A03 is unverifiable here and "
        "A02 measures the built-in eight instead"
    )


def a04_bad_layers(ctx: Context) -> str:
    from qgis.core import QgsProject, QgsVectorLayer  # pyright: ignore[reportMissingImports]

    table = QgsVectorLayer("None?field=p5:integer", "a04 table", "memory")
    require(QgsProject.instance().addMapLayer(table) is not None, "the table layer was not added")
    invalid = QgsVectorLayer(str(ctx.fixtures.root / "a04 missing.gpkg"), "a04 invalid", "ogr")
    require(
        QgsProject.instance().addMapLayer(invalid) is not None, "the invalid layer was not added"
    )
    require(table.isValid() and not invalid.isValid(), "the two probe layers are the wrong shape")

    operations = (
        "layer.list_available_renderers",
        "layer.get_renderer",
        "layer.list_style_categories",
    )
    for operation in operations:
        record = ctx.run(operation, {"layer_id": "a04-no-such-layer"})
        equal(record.status, ExecutionStatus.FAILED, f"{operation}: an unknown id")
        equal(error_code(record), LAYER_NOT_FOUND, f"{operation}: the code for an unknown id")
    # 矢量表：样式读写对它都有定义（属性表也有字段、表单、属性表设置），但它在图例里没有勾选框，
    # 也没有渲染器，所以渲染器操作拒。
    # A vector table: the style reads and writes are all defined for it (a table still has fields,
    # forms and attribute-table settings), while it has no legend checkbox and no renderer, so the
    # renderer operations refuse it.
    table_visibility = ctx.run("layer.set_visibility", {"layer_id": table.id(), "visible": False})
    equal(table_visibility.status, ExecutionStatus.FAILED, "set_visibility on a vector table")
    equal(error_code(table_visibility), INVALID_LAYER, "the code for a table's visibility")
    for operation in ("layer.list_style_categories", "layer.get_qml"):
        record = ctx.run(operation, {"layer_id": table.id()})
        equal(record.status, ExecutionStatus.SUCCEEDED, f"{operation} on a vector table")
    for operation in ("layer.list_available_renderers", "layer.get_renderer"):
        record = ctx.run(operation, {"layer_id": table.id()})
        equal(record.status, ExecutionStatus.FAILED, f"{operation} on a vector table")
        equal(error_code(record), INVALID_LAYER, f"{operation}: the code for a vector table")
    rendered = ctx.run(
        "layer.set_renderer",
        {"layer_id": table.id(), "renderer": '<renderer-v2 type="nullSymbol"/>'},
    )
    equal(rendered.status, ExecutionStatus.FAILED, "layer.set_renderer on a vector table")
    equal(error_code(rendered), INVALID_LAYER, "layer.set_renderer: the code for a vector table")
    # 无效图层：样式读写没有有效性这道门（属性表都对它有），但它自己的渲染器已经没了，
    # 所以渲染器读和设置照样拒。
    # An invalid layer: the style reads and writes have no validity gate (a table passes them too),
    # while its own renderer is gone, so the renderer reads and the setter still refuse.
    for operation in ("layer.list_style_categories", "layer.get_qml"):
        record = ctx.run(operation, {"layer_id": invalid.id()})
        equal(record.status, ExecutionStatus.SUCCEEDED, f"{operation} on an invalid layer")
    for operation in ("layer.list_available_renderers", "layer.get_renderer"):
        record = ctx.run(operation, {"layer_id": invalid.id()})
        equal(record.status, ExecutionStatus.FAILED, f"{operation} on an invalid layer")
        equal(error_code(record), INVALID_LAYER, f"{operation}: the code for an invalid layer")
    return (
        "an unknown id is LAYER_NOT_FOUND on all three reads; a vector table answers "
        "list_style_categories and get_qml while its visibility and all three renderer "
        "operations are INVALID_LAYER (no checkbox, no renderer); an invalid layer keeps the style "
        "reads and refuses the renderer ones, because its renderer is gone with its source"
    )


def a05_stable_result(ctx: Context) -> str:
    vector_id = cities_layer(ctx, "a05 point")
    raster_id = raster_layer(ctx, "a05 raster", ctx.fixtures.raster3)

    counts = {}
    for label, layer_id in (("vector", vector_id), ("raster", raster_id)):
        first = ctx.run("layer.list_available_renderers", {"layer_id": layer_id})
        equal(first.status, ExecutionStatus.SUCCEEDED, f"{label}: status")
        result = result_of(first)
        equal(sorted(result), ["renderer_types"], f"{label}: the result fields")
        types = result["renderer_types"]
        equal(len(types), len(set(types)), f"{label}: duplicates in {types}")
        second = ctx.run("layer.list_available_renderers", {"layer_id": layer_id})
        equal(result_of(second), result, f"{label}: a repeated call differs")
        counts[label] = len(types)
    return (
        f"no duplicates and identical on a repeated call, with only renderer_types in the result "
        f"(vector {counts['vector']} types, raster {counts['raster']} types)"
    )


def a06_get_renderer(ctx: Context) -> str:
    from qgis.core import QgsHeatmapRenderer  # pyright: ignore[reportMissingImports]

    vector_id = cities_layer(ctx, "a06 point")
    raster_id = raster_layer(ctx, "a06 raster", ctx.fixtures.raster3)

    for layer_id in (vector_id, raster_id):
        record = ctx.run("layer.get_renderer", {"layer_id": layer_id})
        equal(record.status, ExecutionStatus.SUCCEEDED, f"{layer_id}: status")
        result = result_of(record)
        equal(sorted(result), ["layer_id", "renderer_type", "renderer_xml"], "the result fields")
        renderer = project_layer(layer_id).renderer()
        equal(result["renderer_type"], renderer.type(), "the live type")
        # 交回来的文档就是渲染器自己那份：把它解析回来必须与原生的 save()/writeXml() 一致。
        # The document handed back is the renderer's own: parsed back it must match the native
        # save()/writeXml() output.
        element = xml_root(result["renderer_xml"])
        if isinstance(renderer, QgsHeatmapRenderer) or result["renderer_type"] == "heatmapRenderer":
            equal(element.tag, "renderer-v2", "the vector document's root")
        if result["renderer_type"] == "multibandcolor":
            equal(element.tag, "rasterrenderer", "the raster document's root")
        equal(element.get("type"), result["renderer_type"], "the type inside the document")

    project_layer(vector_id).setRenderer(QgsHeatmapRenderer())
    equal(installed_renderer(ctx, vector_id), "heatmapRenderer", "a renderer QGIS built elsewhere")
    document = ctx.run("layer.get_renderer", {"layer_id": vector_id})
    equal(
        result_of(document)["renderer_xml"],
        _document_of(project_layer(vector_id).renderer()),
        "the document of a renderer this plugin never built",
    )
    qml = qml_of(ctx, vector_id, ["Symbology"])
    require("heatmapRenderer" in qml, "the heatmap configuration is not in the QML export")
    return (
        "both layer kinds report the live renderer().type() plus the renderer's own document "
        "(rooted at renderer-v2 for a vector and rasterrenderer for a raster); a heatmapRenderer "
        "QGIS built elsewhere reads back byte-identical to its native save()"
    )


def _document_of(renderer: Any) -> str:
    """一个原生渲染器自己的那份文档，用来和 operation 交回来的那份对比。

    A native renderer's own document, to compare with the one the operation hands back.
    """

    from qgis.core import QgsRasterRenderer, QgsReadWriteContext  # pyright: ignore[reportMissingImports]
    from qgis.PyQt.QtXml import QDomDocument

    document = QDomDocument()
    if isinstance(renderer, QgsRasterRenderer):
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


def _install(ctx: Context, layer_id: str, renderer: Any) -> dict[str, Any]:
    record = set_renderer(ctx, layer_id, _document_of(renderer))
    equal(record.status, ExecutionStatus.SUCCEEDED, f"layer.set_renderer failed: {record.error}")
    return result_of(record)


def v01_null_symbol(ctx: Context) -> str:
    from qgis.core import QgsNullSymbolRenderer, QgsProject  # pyright: ignore[reportMissingImports]

    layer_id = sub_layer(ctx, "v01 polygon", fixtures.PARCELS_LAYER)
    before = result_of(ctx.run("layer.get_info", {"layer_id": layer_id}))
    QgsProject.instance().setDirty(False)

    result = _install(ctx, layer_id, QgsNullSymbolRenderer())
    equal(result["renderer_type"], "nullSymbol", "the reported type")
    equal(project_layer(layer_id).renderer().type(), "nullSymbol", "the installed native type")
    equal(installed_renderer(ctx, layer_id), "nullSymbol", "layer.get_renderer")

    after = result_of(ctx.run("layer.get_info", {"layer_id": layer_id}))
    for field in ("visible", "effective_visible", "in_layer_tree"):
        equal(after[field], before[field], f"the layer's {field}")
    node = QgsProject.instance().layerTreeRoot().findLayer(layer_id)
    require(
        node is not None and node.isVisible(),
        "the renderer changed the layer tree's visibility",
    )
    require(QgsProject.instance().isDirty(), "layer.set_renderer left the project clean")
    return (
        "a nullSymbol document installed the layer's own renderer, visibility and layer-tree "
        "presence unchanged, and the project is dirty afterwards although the native setter "
        "alone does not mark it (measured)"
    )


def v02_single_symbol(ctx: Context) -> str:
    from qgis.core import (  # pyright: ignore[reportMissingImports]
        QgsMarkerSymbol,
        QgsSingleSymbolRenderer,
    )

    layer_id = cities_layer(ctx, "v02 point")
    symbol = QgsMarkerSymbol.createSimple({"color": "#1188cc", "size": "3.5"})
    _install(ctx, layer_id, QgsSingleSymbolRenderer(symbol))

    renderer = project_layer(layer_id).renderer()
    equal(renderer.type(), "singleSymbol", "the installed type")
    equal(renderer.symbol().color().name(), "#1188cc", "the symbol colour the document carried")
    equal(round(renderer.symbol().size(), 2), 3.5, "the symbol size the document carried")
    return (
        "a singleSymbol document round-tripped through the operation: the layer's own renderer is "
        "a singleSymbol whose marker kept the colour #1188cc and size 3.5 the document carried"
    )


def v03_categorized(ctx: Context) -> str:
    from qgis.core import (  # pyright: ignore[reportMissingImports]
        QgsCategorizedSymbolRenderer,
        QgsFillSymbol,
        QgsRendererCategory,
    )

    layer_id = sub_layer(ctx, "v03 polygon", fixtures.PARCELS_LAYER)
    categories = [
        QgsRendererCategory(1, QgsFillSymbol.createSimple({"color": "#aa3311"}), "one"),
        QgsRendererCategory(2, QgsFillSymbol.createSimple({"color": "#1188aa"}), "two"),
    ]
    _install(ctx, layer_id, QgsCategorizedSymbolRenderer("pop", categories))

    renderer = project_layer(layer_id).renderer()
    equal(renderer.type(), "categorizedSymbol", "the installed type")
    equal(renderer.classAttribute(), "pop", "the key field")
    equal(len(renderer.categories()), 2, "the categories the document carried")
    equal([category.label() for category in renderer.categories()], ["one", "two"], "the labels")
    return (
        "a categorizedSymbol document installed two categories on the pop field, in their own "
        "order, with the labels the document carried"
    )


def v04_graduated(ctx: Context) -> str:
    from qgis.core import (  # pyright: ignore[reportMissingImports]
        QgsFillSymbol,
        QgsGraduatedSymbolRenderer,
        QgsRendererRange,
    )

    layer_id = sub_layer(ctx, "v04 polygon", fixtures.PARCELS_LAYER)
    ranges = [
        QgsRendererRange(0, 10, QgsFillSymbol.createSimple({"color": "#eeeecc"}), "low"),
        QgsRendererRange(10, 100, QgsFillSymbol.createSimple({"color": "#996633"}), "high"),
    ]
    _install(ctx, layer_id, QgsGraduatedSymbolRenderer("pop", ranges))

    renderer = project_layer(layer_id).renderer()
    equal(renderer.type(), "graduatedSymbol", "the installed type")
    equal(renderer.classAttribute(), "pop", "the key field")
    equal(
        [(item.lowerValue(), item.upperValue()) for item in renderer.ranges()],
        [(0.0, 10.0), (10.0, 100.0)],
        "the breaks the document carried",
    )
    return (
        "a graduatedSymbol document installed the two breaks (0-10, 10-100) on the pop field, "
        "lower and upper bounds included"
    )


def v05_rules(ctx: Context) -> str:
    from qgis.core import (  # pyright: ignore[reportMissingImports]
        QgsFillSymbol,
        QgsRuleBasedRenderer,
    )

    layer_id = sub_layer(ctx, "v05 polygon", fixtures.PARCELS_LAYER)
    root = QgsRuleBasedRenderer.Rule(None)
    for label, expression in (("big", '"pop" > 50'), ("rest", "ELSE")):
        child = QgsRuleBasedRenderer.Rule(
            QgsFillSymbol.createSimple({"color": "#4477aa"}), filterExp=expression, label=label
        )
        root.appendChild(child)
    _install(ctx, layer_id, QgsRuleBasedRenderer(root))

    renderer = project_layer(layer_id).renderer()
    equal(renderer.type(), "RuleRenderer", "the installed type")
    children = renderer.rootRule().children()
    equal([rule.label() for rule in children], ["big", "rest"], "the rule labels")
    equal(children[0].filterExpression(), '"pop" > 50', "the first rule's filter")
    require(children[1].isElse(), "the ELSE rule did not survive the document")
    return (
        "a RuleRenderer document installed two rules in order, kept the filter expression "
        "'\"pop\" > 50' and turned the final ELSE into an else rule"
    )


def v06_document_moves_between_layers(ctx: Context) -> str:
    from qgis.core import (  # pyright: ignore[reportMissingImports]
        QgsCategorizedSymbolRenderer,
        QgsFillSymbol,
        QgsRendererCategory,
    )

    source_id = sub_layer(ctx, "v06 source", fixtures.PARCELS_LAYER)
    target_id = sub_layer(ctx, "v06 target", fixtures.PARCELS_LAYER)
    categories = [QgsRendererCategory(1, QgsFillSymbol.createSimple({"color": "#336699"}), "one")]
    project_layer(source_id).setRenderer(QgsCategorizedSymbolRenderer("pop", categories))
    document = result_of(ctx.run("layer.get_renderer", {"layer_id": source_id}))["renderer_xml"]

    installed = set_renderer(ctx, target_id, document)
    equal(installed.status, ExecutionStatus.SUCCEEDED, f"the moved document: {installed.error}")
    target = project_layer(target_id).renderer()
    equal(target.type(), "categorizedSymbol", "the type on the target layer")
    equal(target.classAttribute(), "pop", "the key field on the target layer")
    reread = result_of(ctx.run("layer.get_renderer", {"layer_id": target_id}))["renderer_xml"]
    equal(reread, document, "the document changed on its way through the operation")
    return (
        "the document read from one polygon layer installed on another unchanged: same type, "
        "same key field, and reading it back gives the same bytes"
    )


def v07_refusals(ctx: Context) -> str:
    from qgis.core import (  # pyright: ignore[reportMissingImports]
        QgsFillSymbol,
        QgsSingleSymbolRenderer,
    )

    layer_id = sub_layer(ctx, "v07 polygon", fixtures.PARCELS_LAYER)
    baseline = QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({"color": "#777777"}))
    _install(ctx, layer_id, baseline)

    refusals = {
        "not xml at all": (layer_id, "p5 not a document"),
        "an unknown type": (layer_id, '<renderer-v2 type="p5NotARenderer"/>'),
        "a raster document": (layer_id, '<rasterrenderer type="singlebandgray"/>'),
        "no type at all": (layer_id, "<renderer-v2/>"),
    }
    for label, (target, document) in refusals.items():
        record = set_renderer(ctx, target, document)
        equal(record.status, ExecutionStatus.FAILED, f"{label}: status")
        equal(error_code(record), INVALID_INPUT, f"{label}: code")
        require(record.result is None, f"{label}: a half-installed renderer was reported")
    equal(project_layer(layer_id).renderer().type(), "singleSymbol", "the renderer after refusals")

    parsed = set_renderer(ctx, layer_id, "<renderer-v2><broken>")
    equal(parsed.status, ExecutionStatus.FAILED, "an unparsable document: status")
    require(
        "line" in message_of(parsed) and "column" in message_of(parsed),
        f"the parser's position is missing from {message_of(parsed)!r}",
    )

    # 能装的东西由 QGIS 的注册表决定，不由这里的一份清单决定。
    # What installs is decided by QGIS's registry, not by a list kept here.
    native = QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({"color": "#123456"}))
    _install(ctx, layer_id, native)
    equal(
        project_layer(layer_id).renderer().symbol().color().name(),
        "#123456",
        "the colour of the renderer installed after the refusals",
    )
    return (
        "unparsable text, an unknown type, a raster document and a missing type are all "
        "FAILED(INVALID_INPUT) and leave the singleSymbol renderer in place; the unparsable one's "
        "message carries the parser's line and column, and a well-formed document installs "
        "afterwards"
    )


def v08_renderer_and_layer_state(ctx: Context) -> str:
    from qgis.core import (  # pyright: ignore[reportMissingImports]
        QgsFillSymbol,
        QgsSingleSymbolRenderer,
    )

    layer_id = sub_layer(ctx, "v08 polygon", fixtures.PARCELS_LAYER)
    before = result_of(ctx.run("layer.get_info", {"layer_id": layer_id}))
    _install(
        ctx,
        layer_id,
        QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({"color": "#abcdef"})),
    )
    after = result_of(ctx.run("layer.get_info", {"layer_id": layer_id}))
    for field in ("layer_id", "name", "kind", "provider", "crs_authid", "valid"):
        equal(after[field], before[field], f"the layer's {field}")
    for field in ("in_layer_tree", "visible", "effective_visible"):
        equal(after[field], before[field], f"the layer's {field}")
    listed = {
        summary["layer_id"]: summary for summary in result_of(ctx.run("layer.list", {}))["layers"]
    }
    equal(listed[layer_id]["name"], before["name"], "the layer's name in the list")
    equal(
        project_layer(layer_id).renderer().symbol().color().name(),
        "#abcdef",
        "the colour the installed document carried",
    )
    return (
        "changing the renderer changes nothing about the layer's identity, visibility or "
        "layer-tree place, while the installed renderer carries the new colour #abcdef"
    )


def r01_single_band_gray(ctx: Context) -> str:
    from qgis.core import (  # pyright: ignore[reportMissingImports]
        QgsContrastEnhancement,
        QgsSingleBandGrayRenderer,
    )

    layer_id = raster_layer(ctx, "r01 raster", ctx.fixtures.raster)
    provider = project_layer(layer_id).dataProvider()
    renderer = QgsSingleBandGrayRenderer(provider, 1)
    enhancement = QgsContrastEnhancement(provider.dataType(1))
    enhancement.setMinimumValue(1.0, False)
    enhancement.setMaximumValue(9.0, False)
    enhancement.setContrastEnhancementAlgorithm(QgsContrastEnhancement.StretchToMinimumMaximum)
    renderer.setContrastEnhancement(enhancement)

    result = _install(ctx, layer_id, renderer)
    equal(result["renderer_type"], "singlebandgray", "the reported type")
    installed = project_layer(layer_id).renderer()
    equal(installed.type(), "singlebandgray", "the installed native type")
    equal(installed.grayBand(), 1, "the band")
    equal(installed.contrastEnhancement().minimumValue(), 1.0, "the stretch minimum")
    equal(installed.contrastEnhancement().maximumValue(), 9.0, "the stretch maximum")
    return (
        "a singlebandgray document installed band 1 with the stretch 1.0-9.0 the document "
        "carried, and layer.get_renderer reports the same type"
    )


def r02_multiband_color(ctx: Context) -> str:
    from qgis.core import QgsMultiBandColorRenderer  # pyright: ignore[reportMissingImports]

    layer_id = raster_layer(ctx, "r02 raster", ctx.fixtures.raster3)
    provider = project_layer(layer_id).dataProvider()

    result = _install(ctx, layer_id, QgsMultiBandColorRenderer(provider, 3, 2, 1))
    equal(result["renderer_type"], "multibandcolor", "the reported type")
    installed = project_layer(layer_id).renderer()
    equal(
        (installed.redBand(), installed.greenBand(), installed.blueBand()),
        (3, 2, 1),
        "the band assignment the document carried",
    )
    return (
        "a multibandcolor document installed the deliberately reversed assignment 3/2/1 on a "
        "three-band raster"
    )


def r03_pseudo_color(ctx: Context) -> str:
    from qgis.core import (  # pyright: ignore[reportMissingImports]
        QgsColorRampShader,
        QgsRasterShader,
        QgsSingleBandPseudoColorRenderer,
    )
    from qgis.PyQt.QtGui import QColor

    layer_id = raster_layer(ctx, "r03 raster", ctx.fixtures.raster)
    provider = project_layer(layer_id).dataProvider()
    ramp = QgsColorRampShader()
    ramp.setColorRampType(QgsColorRampShader.Discrete)
    ramp.setColorRampItemList(
        [
            QgsColorRampShader.ColorRampItem(0.0, QColor("#000000")),
            QgsColorRampShader.ColorRampItem(5.0, QColor("#888888")),
            QgsColorRampShader.ColorRampItem(9.0, QColor("#ffffff")),
        ]
    )
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(ramp)

    result = _install(ctx, layer_id, QgsSingleBandPseudoColorRenderer(provider, 1, shader))
    equal(result["renderer_type"], "singlebandpseudocolor", "the reported type")
    installed = project_layer(layer_id).renderer()
    carried = installed.shader().rasterShaderFunction()
    equal(len(carried.colorRampItemList()), 3, "the stops the document carried")
    equal(
        carried.colorRampType(),
        QgsColorRampShader.Discrete,
        "the interpolation the document carried",
    )
    return (
        "a singlebandpseudocolor document installed three stops and the discrete interpolation "
        "on band 1"
    )


def r04_single_color(ctx: Context) -> str:
    from qgis.core import QgsRasterSingleColorRenderer  # pyright: ignore[reportMissingImports]
    from qgis.PyQt.QtGui import QColor

    layer_id = raster_layer(ctx, "r04 raster", ctx.fixtures.raster)
    provider = project_layer(layer_id).dataProvider()

    result = _install(ctx, layer_id, QgsRasterSingleColorRenderer(provider, 1, QColor("#cc6633")))
    equal(result["renderer_type"], "singlecolor", "the reported type")
    installed = project_layer(layer_id).renderer()
    equal(installed.color().name(), "#cc6633", "the colour the document carried")
    return "a singlecolor document installed #cc6633 on band 1"


def r05_every_registry_type(ctx: Context) -> str:
    from qgis.core import (  # pyright: ignore[reportMissingImports]
        QgsHillshadeRenderer,
        QgsPalettedRasterRenderer,
        QgsRasterContourRenderer,
    )
    from qgis.PyQt.QtGui import QColor

    layer_id = raster_layer(ctx, "r05 raster", ctx.fixtures.raster)
    provider = project_layer(layer_id).dataProvider()
    renderers = {
        "paletted": QgsPalettedRasterRenderer(
            provider,
            1,
            [QgsPalettedRasterRenderer.Class(1, QColor("#112233"), "one")],
        ),
        "hillshade": QgsHillshadeRenderer(provider, 1, 300, 40),
        "contour": QgsRasterContourRenderer(provider),
    }
    installed = {}
    for expected, renderer in renderers.items():
        result = _install(ctx, layer_id, renderer)
        equal(result["renderer_type"], expected, f"{expected}: the reported type")
        installed[expected] = project_layer(layer_id).renderer().type()
    equal(installed, {name: name for name in renderers}, "the installed types")

    # 普通栅格的下拉框里没有 singlebandcolordata，QGIS 自己也把那份渲染器默不作声地丢掉；
    # 报告成功就成了假话，所以这里拒。
    # An ordinary raster's combo has no singlebandcolordata, and QGIS itself drops that renderer
    # without a word; reporting success would be a lie, so it is refused here.
    color_data = '<rasterrenderer type="singlebandcolordata" band="1"/>'
    refused = set_renderer(ctx, layer_id, color_data)
    equal(refused.status, ExecutionStatus.FAILED, "a type the layer does not offer: status")
    equal(error_code(refused), INVALID_INPUT, "a type the layer does not offer: code")
    require(
        "singlebandcolordata" in message_of(refused),
        f"the refused type is not named: {message_of(refused)!r}",
    )
    equal(
        project_layer(layer_id).renderer().type(),
        "contour",
        "the renderer after the refusal",
    )

    unknown = set_renderer(ctx, layer_id, '<rasterrenderer type="p5NotARenderer"/>')
    equal(unknown.status, ExecutionStatus.FAILED, "an unknown raster type: status")
    equal(error_code(unknown), INVALID_INPUT, "an unknown raster type: code")
    return (
        "paletted, hillshade and contour documents all installed and each reported its own type; "
        "a singlebandcolordata document (absent from an ordinary raster's combo, and dropped "
        "silently by QGIS itself) and an unknown type are both FAILED(INVALID_INPUT) with the "
        "previous renderer left in place"
    )


RENDERER_CASES = (
    ("A01", a01_vector_candidates),
    ("A02", a02_raster_candidates),
    ("A03", a03_third_party_candidate),
    ("A03-raster", a03b_raster_third_party),
    ("A04", a04_bad_layers),
    ("A05", a05_stable_result),
    ("A06", a06_get_renderer),
    ("V01", v01_null_symbol),
    ("V02", v02_single_symbol),
    ("V03", v03_categorized),
    ("V04", v04_graduated),
    ("V05", v05_rules),
    ("V06", v06_document_moves_between_layers),
    ("V07", v07_refusals),
    ("V08", v08_renderer_and_layer_state),
    ("R01", r01_single_band_gray),
    ("R02", r02_multiband_color),
    ("R03", r03_pseudo_color),
    ("R04", r04_single_color),
    ("R05", r05_every_registry_type),
)
