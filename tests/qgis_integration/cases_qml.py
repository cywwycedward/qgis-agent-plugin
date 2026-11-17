# 静态分析器自己的解释器里没有 qgis/pydantic，所以本文件只关掉“找不到模块”这一类报告；能不能
# 真的加载由 QGIS 解释器的验收跑证明。
# The analyzer's own interpreter has no qgis/pydantic, so this file turns off only the
# missing-module report; whether it really loads is proven by the QGIS interpreter's run.
# pyright: reportMissingImports=false

"""验收用例：图层样式的 QML 导出。

哪些分类属于哪种图层、点名与不点名分类时的位标记、栅格那条管道，以及导出不落文件。

The acceptance cases for a layer's style as QML: which categories a layer type has, the flags
with and without named categories, the raster pipe, and an export that writes no file.
"""

from __future__ import annotations

from pathlib import Path

import fixtures  # pyright: ignore[reportMissingImports]
from harness import Context, Skip, equal, require  # pyright: ignore[reportMissingImports]
from helpers import (
    cities_layer,
    error_code,
    qml_files,
    qml_of,
    raster_layer,
    result_of,
    style_categories_attribute,
    sub_layer,
    xml_root,
)

from qgis_agent_plugin.core.models import INVALID_INPUT, ExecutionStatus  # pyright: ignore[reportMissingImports]

# 这三张表故意在这里写死，验收拿它们与插件比对，而不是与插件自己的元组比对。
# The three tables are written out here on purpose, so the acceptance compares the plugin against
# them rather than against the plugin's own tuples.
VECTOR_CATEGORY_NAMES = [
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


RASTER_CATEGORY_NAMES = [
    "LayerConfiguration",
    "Symbology",
    "MapTips",
    "Rendering",
    "CustomProperties",
    "Temporal",
    "Elevation",
    "AttributeTable",
    "Notes",
]


def s01_vector_categories(ctx: Context) -> str:
    layers = {
        "point": cities_layer(ctx, "s01 point"),
        "line": sub_layer(ctx, "s01 line", fixtures.ROADS_LAYER),
        "polygon": sub_layer(ctx, "s01 polygon", fixtures.PARCELS_LAYER),
    }
    for geometry, layer_id in layers.items():
        record = ctx.run("layer.list_style_categories", {"layer_id": layer_id})
        equal(record.status, ExecutionStatus.SUCCEEDED, f"{geometry}: status")
        result = result_of(record)
        equal(sorted(result), ["categories", "layer_id"], f"{geometry}: the result fields")
        equal(result["layer_id"], layer_id, f"{geometry}: the layer id")
        equal(result["categories"], VECTOR_CATEGORY_NAMES, f"{geometry}: the 18 categories")
    return (
        "Point, Line and Polygon layers all report the same 18 base categories in a fixed "
        "order, with only layer_id and categories in the result"
    )


def s02_raster_categories(ctx: Context) -> str:
    sources = {
        "s02 one-band": ctx.fixtures.raster,
        "s02 three-band": ctx.fixtures.raster3,
    }
    for name, source in sources.items():
        layer_id = raster_layer(ctx, name, source)
        record = ctx.run("layer.list_style_categories", {"layer_id": layer_id})
        equal(record.status, ExecutionStatus.SUCCEEDED, f"{name}: status")
        categories = result_of(record)["categories"]
        equal(categories, RASTER_CATEGORY_NAMES, f"{name}: the nine categories")
        for required in ("AttributeTable", "Notes"):
            require(required in categories, f"{name}: {required} is missing from {categories}")
        require("Labeling" not in categories, f"{name}: Labeling is not a raster checkbox")
    return (
        "a one-band and a three-band raster both report the same nine categories, including "
        "AttributeTable and Notes and without Labeling, in the order the category model "
        "lists them"
    )


def s03_invalid_categories(ctx: Context) -> str:
    vector_id = sub_layer(ctx, "s03 polygon", fixtures.PARCELS_LAYER)
    raster_id = raster_layer(ctx, "s03 raster")
    # 分类是一个位标记：重复的名字就是一个位，不适用的分类只是一个没人写的位，两者都不是错。
    # The categories are one bit flag: a repeated name is one bit, and a category this layer type
    # never writes is just an unused bit.  Neither is an error.
    accepted = {
        "a repeated category": (vector_id, ["Symbology", "Symbology"]),
        "a vector-only category on a raster": (raster_id, ["Symbology3D"]),
        "a vector-only category on a raster, doubled": (raster_id, ["Fields", "Fields"]),
        "nothing at all": (vector_id, []),
        "nothing at all on a raster": (raster_id, []),
    }
    for label, (layer_id, categories) in accepted.items():
        record = ctx.run("layer.get_qml", {"layer_id": layer_id, "categories": categories})
        equal(record.status, ExecutionStatus.SUCCEEDED, f"{label}: {record.error}")
        require(xml_root(result_of(record)["qml"]) is not None, f"{label}: no document came back")

    unknown = ctx.run(
        "layer.get_qml", {"layer_id": vector_id, "categories": ["p5NotAStyleCategory"]}
    )
    equal(unknown.status, ExecutionStatus.FAILED, "an unknown category name: status")
    equal(error_code(unknown), INVALID_INPUT, "an unknown category name: code")
    require(unknown.result is None, "an unknown category name: a partial export was returned")

    # 零分类导出的是 ``styleCategories=""``（实测），所以断言原始属性而不是集合。
    # A zero-category export writes ``styleCategories=""`` (measured), so the raw attribute is what
    # this asserts.
    empty = ctx.run("layer.get_qml", {"layer_id": vector_id, "categories": []})
    equal(
        xml_root(result_of(empty)["qml"]).get("styleCategories") or "",
        "",
        "the categories an empty list declares",
    )
    return (
        "a repeated category, a vector-only category on a raster and an empty list all SUCCEED and "
        "export a document (the empty list one declares no style categories), while a name outside "
        "the model is FAILED(INVALID_INPUT) with no partial XML"
    )


def s04_symbology_only(ctx: Context) -> str:
    layer_id = sub_layer(ctx, "s04 polygon", fixtures.PARCELS_LAYER)
    scanned = (Path.cwd(), ctx.fixtures.root.parent)
    files_before = qml_files(*scanned)

    record = ctx.run("layer.get_qml", {"layer_id": layer_id, "categories": ["Symbology"]})
    equal(record.status, ExecutionStatus.SUCCEEDED, f"layer.get_qml failed: {record.error}")
    result = result_of(record)
    equal(sorted(result), ["layer_id", "qml"], "the result fields")
    qml = result["qml"]
    root = xml_root(qml)
    equal(root.tag, "qgis", "the QML root element")
    equal(root.get("styleCategories"), "Symbology", "the categories the root declares")
    require("renderer-v2" in qml, "the exported XML carries no renderer")
    equal(qml_files(*scanned), files_before, "the .qml files this export created")
    return (
        f"a Symbology-only export is valid XML ({len(qml)} bytes) whose qgis root declares "
        'styleCategories="Symbology" and carries a renderer-v2 node, and the run created no '
        ".qml file"
    )


def s05_multiple_and_raster_pipe(ctx: Context) -> str:
    vector_id = sub_layer(ctx, "s05 polygon", fixtures.PARCELS_LAYER)
    raster_id = raster_layer(ctx, "s05 raster", ctx.fixtures.raster3)

    combined = qml_of(ctx, vector_id, ["Symbology", "Labeling"])
    equal(style_categories_attribute(combined), {"Symbology", "Labeling"}, "the combined flags")

    symbology = qml_of(ctx, raster_id, ["Symbology"])
    rendering = qml_of(ctx, raster_id, ["Rendering"])
    equal(style_categories_attribute(symbology), {"Symbology"}, "a raster Symbology export")
    equal(style_categories_attribute(rendering), {"Rendering"}, "a raster Rendering export")
    for label, qml in (("Symbology", symbology), ("Rendering", rendering)):
        require("<pipe" in qml, f"the raster {label} export carries no Raster Pipe")
        require("rasterrenderer" in qml, f"the raster {label} export carries no raster renderer")
    require(
        symbology.count("<pipe") == rendering.count("<pipe"),
        "the two overlapping categories exported a different number of pipes",
    )
    return (
        "the vector export bit-combines Symbology|Labeling in its root attribute, and a raster "
        "exports its Raster Pipe for Symbology and for Rendering alike (QGIS 3.44's own coupling), "
        "with the requested category in styleCategories"
    )


def s06_export_failure(ctx: Context) -> str:
    raise Skip(
        "no real layer can be made to fail exportNamedStyle here, so S06's sanitised "
        "INTERNAL_ERROR cannot be observed: a valid vector layer (with and without a renderer) "
        "and a valid raster all return an empty message (measured), a layer that cannot export is "
        "refused earlier as INVALID_LAYER, and a renderer written in Python to make save() fail "
        "ends the process (0xC0000409, sip ownership) — that branch must not be a case. The "
        "unpacking half of S06 is proven by S04/S05 succeeding (the binding returns str, not "
        "(message, ok)); the failure branch stays covered by "
        "tests/test_operations_layer.py's fake"
    )


QML_CASES = (
    ("S01", s01_vector_categories),
    ("S02", s02_raster_categories),
    ("S03", s03_invalid_categories),
    ("S04", s04_symbology_only),
    ("S05", s05_multiple_and_raster_pipe),
    ("S06", s06_export_failure),
)
