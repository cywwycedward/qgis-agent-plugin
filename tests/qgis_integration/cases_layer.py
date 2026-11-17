# 静态分析器自己的解释器里没有 qgis/pydantic，所以本文件只关掉“找不到模块”这一类报告；能不能
# 真的加载由 QGIS 解释器的验收跑证明。
# The analyzer's own interpreter has no qgis/pydantic, so this file turns off only the
# missing-module report; whether it really loads is proven by the QGIS interpreter's run.
# pyright: reportMissingImports=false

"""验收用例：图层装载、列表、可见性与字段求和。

一条矢量或栅格源怎么进图例、图例的顺序、两个可见性标志，以及跑在后台线程上的字段求和。

The acceptance cases for layers: how a vector or raster source reaches the legend, the order
the legend shows, the two visibility flags, and the field sum that runs off the main thread.
"""

from __future__ import annotations

import fixtures  # pyright: ignore[reportMissingImports]
from harness import Context, equal, require  # pyright: ignore[reportMissingImports]
from helpers import cities_layer, error_code, project_layer_ids, reset_project, result_of

from qgis_agent_plugin.core.models import (  # pyright: ignore[reportMissingImports]
    INVALID_FIELD_TYPE,
    INVALID_LAYER,
    LAYER_NOT_IN_TREE,
    ExecutionStatus,
)


def at_20_invalid_source(ctx: Context) -> str:
    layers_before = project_layer_ids()
    record = ctx.run(
        "layer.add",
        {"kind": "vector", "source_path": str(ctx.fixtures.corrupt_gpkg), "name": "at20"},
    )
    equal(record.status, ExecutionStatus.FAILED, "layer.add from a corrupt GeoPackage")
    equal(error_code(record), INVALID_LAYER, "the code for an invalid source")
    equal(project_layer_ids(), layers_before, "the layers after the refused add")
    return (
        f"FAILED(INVALID_LAYER) for a corrupt GeoPackage; the project still holds "
        f"{len(layers_before)} layer(s)"
    )


def at_21_layer_without_tree_node(ctx: Context) -> str:
    """加载来的工程里可以有不图层里的图层；两种读照样给答案，切可见性无从下手。

    A loaded project can hold a layer that is not in the legend; both reads still answer,
    and toggling its visibility has nothing to act on.
    """

    from qgis.core import QgsProject, QgsVectorLayer

    reset_project(ctx)
    # 这个形状只有工程文件能表达：图层进注册表、不进图层树。
    # The one shape only a project file expresses: in the registry, absent from the tree.
    layer = QgsVectorLayer(
        f"{ctx.fixtures.cities}|layername={fixtures.CITIES_LAYER}",
        "at21 not in the tree",
        "ogr",
    )
    require(layer.isValid(), "the probe layer is not valid")
    require(QgsProject.instance().addMapLayer(layer, False) is not None, "the layer was not added")
    require(
        QgsProject.instance().layerTreeRoot().findLayer(layer.id()) is None,
        "the probe layer ended up in the tree",
    )

    rows = result_of(ctx.run("layer.list"))["layers"]
    row = next(item for item in rows if item["layer_id"] == layer.id())
    equal(row["in_layer_tree"], False, "in_layer_tree")
    equal(row["visible"], None, "visible")
    equal(row["effective_visible"], None, "effective_visible")

    record = ctx.run("layer.set_visibility", {"layer_id": layer.id(), "visible": True})
    equal(record.status, ExecutionStatus.FAILED, "set_visibility on a tree-less layer")
    equal(error_code(record), LAYER_NOT_IN_TREE, "the code for a tree-less layer")
    return (
        "a registered layer with no layer tree node lists as in_layer_tree=false, "
        "visible=null and effective_visible=null, and set_visibility is "
        "FAILED(LAYER_NOT_IN_TREE)"
    )


def at_21b_group_hides_layer(ctx: Context) -> str:
    from qgis.core import QgsProject  # pyright: ignore[reportMissingImports]

    reset_project(ctx)
    layer_id = cities_layer(ctx, "at21b cities")
    root = QgsProject.instance().layerTreeRoot()
    group = root.addGroup("at21b group")
    node = root.findLayer(layer_id)
    require(node is not None, "the layer has no layer tree node")
    group.addChildNode(node.clone())
    root.removeChildNode(node)
    group.setItemVisibilityChecked(False)

    info = result_of(ctx.run("layer.get_info", {"layer_id": layer_id}))
    equal(info["visible"], True, "the layer's own check state")
    equal(info["effective_visible"], False, "the layer's effective visibility")

    record = ctx.run("layer.set_visibility", {"layer_id": layer_id, "visible": True})
    equal(record.status, ExecutionStatus.SUCCEEDED, "set_visibility on a hidden layer")
    equal(result_of(record)["effective_visible"], True, "the layer after the check")
    equal(group.itemVisibilityChecked(), True, "the parent group's check state")
    equal(QgsProject.instance().isDirty(), True, "the project after a legend write")
    return (
        "visible=true while effective_visible=false under an unchecked group; the legend's "
        "check acts on the ancestors too, so set_visibility(true) left the layer and its group "
        "checked and marked the project dirty"
    )


def at_22_visibility_idempotent(ctx: Context) -> str:
    from qgis.core import QgsProject  # pyright: ignore[reportMissingImports]

    reset_project(ctx)
    layer_id = cities_layer(ctx, "at22 cities")
    QgsProject.instance().setDirty(False)
    first = ctx.run("layer.set_visibility", {"layer_id": layer_id, "visible": False})
    equal(first.status, ExecutionStatus.SUCCEEDED, "the first set_visibility")
    equal(result_of(first)["visible"], False, "the layer after the first call")
    equal(QgsProject.instance().isDirty(), True, "the project after a real change")

    QgsProject.instance().setDirty(False)
    second = ctx.run("layer.set_visibility", {"layer_id": layer_id, "visible": False})
    equal(second.status, ExecutionStatus.SUCCEEDED, "the repeated set_visibility")
    equal(result_of(second)["visible"], False, "the layer after the repeated call")
    equal(QgsProject.instance().isDirty(), False, "the project after an unchanged rewrite")
    return (
        "the same set_visibility twice: SUCCEEDED and the project marked dirty, then SUCCEEDED "
        "with the unchanged rewrite left undirty"
    )


def at_28_layer_add_sub_layer(ctx: Context) -> str:
    """GeoPackage 里指定名字的一张表，经 OGR 编码器加进来。

    One named table in a GeoPackage, reached through the OGR encoder.
    """

    from qgis.core import QgsProject  # pyright: ignore[reportMissingImports]

    reset_project(ctx)
    source = ctx.fixtures.spaced  # 文件名本身就带空格，故意如此 / its own name has a space
    layer_id = ctx.add_layer(source, "at28 roads", sub_layer=fixtures.ROADS_LAYER)
    layer = QgsProject.instance().mapLayer(layer_id)
    if layer is None:
        raise AssertionError("layer.add reported SUCCEEDED but registered no layer")

    equal(layer.providerType(), "ogr", "the provider of a sub-layer add")
    equal(layer.isValid(), True, "the layer is usable")
    # 这份 GeoPackage 是五点一线，所以要素数证明编码器选的是具名表。
    # The GeoPackage holds five points and one line, so the feature count proves the encoder picked
    # the named table.
    equal(layer.featureCount(), 1, "the features of the roads table")
    equal(
        result_of(ctx.run("layer.get_info", {"layer_id": layer_id}))["provider"],
        "ogr",
        "the provider layer.get_info reports",
    )
    # 一个 provider 从未报过的子图层名不是答案（选择框点不出它）。
    # A sub-layer name the provider never reported is not an answer (the picker cannot offer it).
    missing = ctx.run(
        "layer.add",
        {
            "kind": "vector",
            "source_path": str(source),
            "name": "at28 missing",
            "sub_layer": "p5 no such table",
        },
    )
    equal(missing.status, ExecutionStatus.FAILED, "layer.add with a name the provider lacks")
    equal(error_code(missing), INVALID_LAYER, "the code for a name the provider lacks")
    equal(
        len(QgsProject.instance().mapLayers()),
        1,
        "the project after a refused sub-layer name",
    )
    # 栅格也有子数据集清单：同一个源、kind=raster、点名那个子数据集也能加（A5）。
    # A raster has a subdataset list too: the same kind of call with kind=raster and the
    # name the provider reports (A5).
    from qgis.core import QgsProviderRegistry

    gdal = QgsProviderRegistry.instance().providerMetadata("gdal")
    raster_name = gdal.querySublayers(str(ctx.fixtures.raster))[0].name()
    raster_record = ctx.run(
        "layer.add",
        {
            "kind": "raster",
            "source_path": str(ctx.fixtures.raster),
            "name": "at28 raster sub",
            "sub_layer": raster_name,
        },
    )
    equal(
        raster_record.status,
        ExecutionStatus.SUCCEEDED,
        f"layer.add(kind='raster', sub_layer={raster_name!r}): {raster_record.error}",
    )
    raster_layer_id = result_of(raster_record)["layer_id"]
    equal(
        QgsProject.instance().mapLayer(raster_layer_id).providerType(),
        "gdal",
        "the provider of a raster sub-dataset add",
    )
    return (
        f"layer.add(sub_layer='{fixtures.ROADS_LAYER}') on {source.name!r} SUCCEEDED, provider "
        f"ogr, featureCount 1 (cities holds 5): the provider's own sublayer URI chose the named "
        f"table; a sub_layer the provider never reported is FAILED(INVALID_LAYER) and adds "
        f"nothing; and kind='raster' with the name gdal reports ({raster_name!r}) SUCCEEDED as "
        f"a gdal layer"
    )


def at_44_field_sum_refuses_a_non_finite_value(ctx: Context) -> str:
    """NaN 和 Infinity 不是 JSON，所以结果里会带上它们的求和只能拒。

    NaN and Infinity are not JSON, so a sum that would carry them is refused.
    """

    from qgis.core import QgsFeature, QgsProject, QgsVectorLayer  # pyright: ignore[reportMissingImports]

    reset_project(ctx)
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=n:double", "at44 non finite", "memory")
    features = []
    for value in (1.0, float("nan"), float("inf")):
        feature = QgsFeature(layer.fields())
        feature.setAttributes([value])
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)

    submitted = ctx.submit("layer.sum_numeric_field", {"layer_id": layer.id(), "field_name": "n"})
    record = ctx.wait_terminal(submitted.execution_id)

    equal(record.status, ExecutionStatus.FAILED, "the sum of a field holding NaN")
    equal(error_code(record), INVALID_FIELD_TYPE, "the code for a non-finite value")
    error = record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    require("non-finite" in error.message, f"the message: {error.message!r}")
    require(record.result is None, "a failed sum delivered a result")
    reset_project(ctx)
    return (
        "a memory layer with values (1.0, nan, inf): FAILED(INVALID_FIELD_TYPE) with 'the field "
        "holds a non-finite ...' and no result. The audit measured total=nan in a SUCCEEDED "
        "record, and json.dumps(..., allow_nan=False) refuses that record"
    )


LAYER_CASES = (
    ("AT-20", at_20_invalid_source),
    ("AT-21", at_21_layer_without_tree_node),
    ("AT-21B", at_21b_group_hides_layer),
    ("AT-22", at_22_visibility_idempotent),
    ("AT-28", at_28_layer_add_sub_layer),
    ("AT-44", at_44_field_sum_refuses_a_non_finite_value),
)
