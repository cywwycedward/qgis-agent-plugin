# 静态分析器自己的解释器里没有 qgis/pydantic，所以本文件只关掉“找不到模块”这一类报告；能不能
# 真的加载由 QGIS 解释器的验收跑证明。
# The analyzer's own interpreter has no qgis/pydantic, so this file turns off only the
# missing-module report; whether it really loads is proven by the QGIS interpreter's run.
# pyright: reportMissingImports=false

"""验收用例共用的说法：一条记录的错误码与结果、一个真实图层、一份 QML。

用例只读记录与工程，不读插件内部；这些读法收在这里，一条用例因此只剩它自己的判据。

The vocabulary every acceptance case speaks: a record's code and result, a real layer, a
layer's QML.  A case reads records and the project and nothing inside the plugin, and keeping
those reads here leaves a case with nothing but its own criterion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fixtures  # pyright: ignore[reportMissingImports]
from harness import Context, equal, pump, require  # pyright: ignore[reportMissingImports]

from qgis_agent_plugin.core.execution_store import TERMINAL_STATUSES  # pyright: ignore[reportMissingImports]
from qgis_agent_plugin.core.models import ExecutionRecord, ExecutionStatus, FrameworkError  # pyright: ignore[reportMissingImports]

LIVE_STATUSES = [
    ExecutionStatus.QUEUED,
    ExecutionStatus.PREPARING,
    ExecutionStatus.SCHEDULED,
    ExecutionStatus.RUNNING,
    ExecutionStatus.CANCEL_REQUESTED,
    ExecutionStatus.FINALIZING,
]


ALL_STATUSES = [*LIVE_STATUSES, *TERMINAL_STATUSES]


def project_layers() -> dict[str, Any]:
    from qgis.core import QgsProject  # pyright: ignore[reportMissingImports]

    return QgsProject.instance().mapLayers()


def project_layer_ids() -> list[str]:
    return sorted(project_layers())


def reset_project(ctx: Context) -> None:
    """给用例自己的准备步骤一个干净工程：直接清空，不走任何操作。

    A clean project for a case's own setup, cleared directly rather than by an operation.
    """

    from qgis.core import QgsProject  # pyright: ignore[reportMissingImports]

    QgsProject.instance().clear()
    pump(ctx.app, 0.05)


def cities_layer(ctx: Context, name: str = "cities") -> str:
    """用插件自己的 ``layer.add`` 加一个真实矢量图层，返回图层 id。

    A real vector layer added through the plugin's own ``layer.add``.
    """

    return ctx.add_layer(ctx.fixtures.cities, name, sub_layer=fixtures.CITIES_LAYER)


def error_code(record: ExecutionRecord) -> str:
    error = record.error
    if error is None:
        raise AssertionError(f"{record.status} record without an error code")
    return error.code


def result_of(record: ExecutionRecord) -> dict[str, Any]:
    result = record.result
    if result is None:
        raise AssertionError(f"{record.status} record without a result")
    return result


def refused(call: Any) -> str:
    """``call`` 抛出的 ``FrameworkError`` 错误码；它没被拒，用例就失败。

    The code of the ``FrameworkError`` ``call`` raised, or a failed assertion.
    """

    try:
        call()
    except FrameworkError as error:
        return str(error.code)
    raise AssertionError("the call was not refused")


def all_record_ids(ctx: Context) -> set[str]:
    return set(ctx.engine._store.ids(ALL_STATUSES))


def manager_task_count(ctx: Context) -> int:
    manager = ctx.runner._manager
    return 0 if manager is None else len(manager.tasks())


def processing_algorithms() -> list[Any]:
    """当前 QGIS Processing 注册表里的全部算法，按注册表自己的顺序。

    Every algorithm the live QGIS Processing registry holds, in its own order.
    """

    from qgis.core import QgsApplication  # pyright: ignore[reportMissingImports]

    return list(QgsApplication.processingRegistry().algorithms())


def algorithm_info(ctx: Context, algorithm_id: str) -> dict[str, Any]:
    record = ctx.run("processing.get_algorithm_info", {"algorithm_id": algorithm_id})
    equal(record.status, ExecutionStatus.SUCCEEDED, f"get_algorithm_info({algorithm_id})")
    return result_of(record)


def parameters_by_name(info: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {parameter["name"]: parameter for parameter in info["parameters"]}


def run_algorithm(ctx: Context, algorithm_id: str, parameters: dict[str, Any]) -> ExecutionRecord:
    """一次 ``processing.run_algorithm`` 调用，返回它的执行记录。

    One ``processing.run_algorithm`` call, as its record.
    """

    return ctx.run(
        "processing.run_algorithm", {"algorithm_id": algorithm_id, "parameters": parameters}
    )


def outputs_of(record: ExecutionRecord) -> dict[str, Any]:
    return result_of(record)["outputs"]


def output_layer(ctx: Context, record: ExecutionRecord, key: str) -> Any:
    """取某个输出键指向的工程图层；id 指向工程里没有的图层就是用例失败。

    The project layer one output key names; an id the project does not hold is a failure.
    """

    layer_id = outputs_of(record)[key]
    require(isinstance(layer_id, str), f"{key} names {layer_id!r}, which is not a layer id")
    layer = project_layers().get(layer_id)
    require(layer is not None, f"{key} names {layer_id!r}, which is not a project layer")
    return layer


def sub_layer(ctx: Context, name: str, layer_name: str) -> str:
    """GeoPackage 测试数据里的某一张表，走插件自己的 ``layer.add`` 加进来。

    A layer of the GeoPackage fixture, added through the plugin's own ``layer.add``.
    """

    return ctx.add_layer(ctx.fixtures.cities, name, sub_layer=layer_name)


def raster_layer(ctx: Context, name: str, source: Path | None = None) -> str:
    """走插件自己的 ``layer.add`` 加一个栅格图层，返回图层 id。

    A raster layer added through the plugin's own ``layer.add``.
    """

    record = ctx.run(
        "layer.add",
        {
            "kind": "raster",
            "source_path": str(ctx.fixtures.raster if source is None else source),
            "name": name,
        },
    )
    require(record.status is ExecutionStatus.SUCCEEDED, f"layer.add({name}) failed: {record.error}")
    return str((record.result or {})["layer_id"])


def project_layer(layer_id: str) -> Any:
    layer = project_layers().get(layer_id)
    require(layer is not None, f"no project layer {layer_id!r}")
    return layer


def installed_renderer(ctx: Context, layer_id: str) -> str:
    """经引擎问一次 ``layer.get_renderer``，取回它报告的渲染器类型。

    What ``layer.get_renderer`` reports for a layer, through the engine.
    """

    record = ctx.run("layer.get_renderer", {"layer_id": layer_id})
    require(record.status is ExecutionStatus.SUCCEEDED, f"layer.get_renderer: {record.error}")
    return str(result_of(record)["renderer_type"])


def set_renderer(ctx: Context, layer_id: str, document: str) -> ExecutionRecord:
    return ctx.run("layer.set_renderer", {"layer_id": layer_id, "renderer": document})


def qml_of(ctx: Context, layer_id: str, categories: list[str] | None = None) -> str:
    arguments: dict[str, Any] = {"layer_id": layer_id}
    if categories is not None:
        arguments["categories"] = categories
    record = ctx.run("layer.get_qml", arguments)
    equal(record.status, ExecutionStatus.SUCCEEDED, f"layer.get_qml: {record.error}")
    return str(result_of(record)["qml"])


def xml_root(qml: str) -> Any:
    """导出 QML 字符串的根元素；导出的不是合法 XML 就是用例失败。

    The root element of an exported QML string; invalid XML is a failed case.
    """

    import xml.etree.ElementTree as ElementTree

    try:
        return ElementTree.fromstring(qml)
    except ElementTree.ParseError as error:
        raise AssertionError(f"the QML export is not valid XML: {error}") from None


def style_categories_attribute(qml: str) -> set[str]:
    return set((xml_root(qml).get("styleCategories") or "").split("|"))


def qml_files(*roots: Path) -> list[str]:
    """给定根目录下的所有 ``.qml`` 文件（默认当前工作目录）。

    ``layer.get_qml`` 根本没有文件路径参数，所以一次运行必须让扫到的根目录保持原样；运行目录与
    测试数据根目录都会被扫。

    Every ``.qml`` file under the given roots (the working directory by default).
    ``layer.get_qml`` has no file path at all, so a run must leave every scanned root
    untouched; the run's own directory and the fixture root are both scanned.
    """

    found: list[str] = []
    for root in roots or (Path.cwd(),):
        found.extend(str(path) for path in Path(root).rglob("*.qml"))
    return sorted(found)


def message_of(record: ExecutionRecord) -> str:
    return "" if record.error is None else record.error.message
