# 静态分析器自己的解释器里没有 qgis/pydantic，所以本文件只关掉“找不到模块”这一类报告；能不能
# 真的加载由 QGIS 解释器的验收跑证明。
# The analyzer's own interpreter has no qgis/pydantic, so this file turns off only the
# missing-module report; whether it really loads is proven by the QGIS interpreter's run.
# pyright: reportMissingImports=false

"""验收用例集：一条用例一个函数，按用例编号登记在 ``CASES`` 里，只返回一行证据。

期待落空就抛 ``AssertionError``，所以用例不可能顺手通过；这台机器给不了的环境抛 ``Skip`` 并带上
原因，跳过永远不算通过。用例需要的状态主要靠插件自己的操作、在生成的测试数据上建立；少数准备步骤
直接动 ``QgsProject``（清空工程、搬图层树节点），因为那些形状正是要被测的东西。

Each case is registered in ``CASES`` under its id and returns one evidence line.  An
expectation that does not hold raises ``AssertionError``, so a case can never pass by
accident; an environment this machine cannot provide raises ``Skip`` with the reason,
and a skip never counts as a pass.  Every case builds its state through the plugin's own
operations against the generated fixtures only: a destructive case is never about a
user's own project.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from functools import partial
from pathlib import Path
from typing import Any

import fixtures
import pydantic
from harness import (
    FINALIZE_CANCEL,
    GATES,
    PROBE_LOG,
    Context,
    DuplicateProjectInfo,
    Skip,
    equal,
    flush_deferred,
    pump,
    pump_until,
    qt_deleted,
    require,
    require_processing,
)
from pydantic import ValidationError

from qgis_agent_plugin.core.config import Settings
from qgis_agent_plugin.core.context import ProjectLifecycle
from qgis_agent_plugin.core.engine import OperationEngine
from qgis_agent_plugin.core.execution_store import TERMINAL_STATUSES
from qgis_agent_plugin.core.models import (
    DUPLICATE_OPERATION,
    ENGINE_CLOSING,
    EXECUTION_NOT_FOUND,
    INTERNAL_ERROR,
    INVALID_FIELD_TYPE,
    INVALID_INPUT,
    INVALID_LAYER,
    LAYER_NOT_FOUND,
    OUTPUT_VALIDATION_FAILED,
    PATH_NOT_ALLOWED,
    PROJECT_OPEN_FAILED,
    PROJECT_REPLACE_REQUIRED,
    STALE_PROJECT,
    TARGET_EXISTS,
    TASK_SUBMISSION_FAILED,
    UNSUPPORTED_EXECUTION_MODE,
    WRONG_THREAD,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionStrategy,
    FrameworkError,
    OperationSpec,
)
from qgis_agent_plugin.core.registry import OperationRegistry
from qgis_agent_plugin.core.runners.qgs_task import OperationTaskRunner, _dependent_layers
from qgis_agent_plugin.operations import register_all
from qgis_agent_plugin.operations.layer import (
    LayerSetVisibilityParams,
    LayerSumNumericField,
    SumNumericFieldParams,
)
from qgis_agent_plugin.operations.project import MAX_PROJECT_DEPTH

LIVE_STATUSES = [
    ExecutionStatus.QUEUED,
    ExecutionStatus.PREPARING,
    ExecutionStatus.SCHEDULED,
    ExecutionStatus.RUNNING,
    ExecutionStatus.CANCEL_REQUESTED,
    ExecutionStatus.FINALIZING,
]

ALL_STATUSES = [*LIVE_STATUSES, *TERMINAL_STATUSES]


# ------------------------------------------------------------------- 辅助函数 / helpers


def project_layers() -> dict[str, Any]:
    from qgis.core import QgsProject

    return QgsProject.instance().mapLayers()


def project_layer_ids() -> list[str]:
    return sorted(project_layers())


def reset_project(ctx: Context) -> None:
    """给用例自己的准备步骤一个干净工程：直接清空，不走任何操作。

    A clean project for a case's own setup, cleared directly rather than by an operation.
    """

    from qgis.core import QgsProject

    QgsProject.instance().clear()
    pump(ctx.app, 0.05)


def cities_layer(ctx: Context, name: str = "cities") -> str:
    """用插件自己的 ``layer.add`` 加一个真实矢量图层，返回图层 id。

    A real vector layer added through the plugin's own ``layer.add``.
    """

    return ctx.add_layer(ctx.fixtures.cities, name)


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

    from qgis.core import QgsApplication

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


def set_renderer(ctx: Context, layer_id: str, config: dict[str, Any]) -> ExecutionRecord:
    return ctx.run("layer.set_renderer", {"layer_id": layer_id, "renderer": config})


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


# --------------------------------------------------------------- 用例 / the cases


def at_01_duplicate_registration(ctx: Context) -> str:
    registry = ctx.registry
    names_before = sorted(spec.name for spec in registry.list_specs())
    equal(len(names_before), 17, "operations registered at start-up")
    original = registry.get("project.get_info")

    code = refused(lambda: registry.register(DuplicateProjectInfo()))

    equal(code, DUPLICATE_OPERATION, "the refusal code")
    require(registry.get("project.get_info") is original, "the original registration was replaced")
    equal(
        sorted(spec.name for spec in registry.list_specs()), names_before, "names after the refusal"
    )
    return (
        f"{code}; project.get_info still {type(original).__name__}; the seventeen specs are "
        "unchanged"
    )


def at_02_invalid_input(ctx: Context) -> str:
    layer_id = cities_layer(ctx, "at02 cities")
    before_visible = result_of(ctx.run("layer.get_info", {"layer_id": layer_id}))["visible"]
    layers_before = project_layer_ids()

    wrong_bool = ctx.run("layer.set_visibility", {"layer_id": layer_id, "visible": "true"})
    equal(wrong_bool.status, ExecutionStatus.FAILED, "a string where a boolean belongs")
    equal(error_code(wrong_bool), INVALID_INPUT, "code for a string where a boolean belongs")

    unknown_key = ctx.run(
        "layer.set_visibility", {"layer_id": layer_id, "visible": True, "not_a_field": 1}
    )
    equal(unknown_key.status, ExecutionStatus.FAILED, "an undeclared input field")
    equal(error_code(unknown_key), INVALID_INPUT, "code for an undeclared input field")

    after_visible = result_of(ctx.run("layer.get_info", {"layer_id": layer_id}))["visible"]
    equal(after_visible, before_visible, "visibility after the refused calls")
    equal(project_layer_ids(), layers_before, "project layers after the refused calls")
    return (
        f"INVALID_INPUT for visible='true' and for an undeclared key; "
        f"visibility still {before_visible}, layer count still {len(layers_before)}"
    )


def at_03_run_sync(ctx: Context) -> str:
    record = ctx.run("project.get_info")
    equal(record.status, ExecutionStatus.SUCCEEDED, "project.get_info status")
    equal(record.strategy, ExecutionStrategy.MAIN_THREAD, "strategy of the record")
    equal(record.progress, 100, "progress of a succeeded record")
    equal(record.project_epoch, ctx.lifecycle.current_epoch, "the epoch the record captured")
    require(record.finished_at is not None, "a terminal record has finished_at")
    exported = record.model_dump_json()
    equal(ExecutionRecord.model_validate_json(exported).status, record.status, "record round trip")
    equal(ctx.record(record.execution_id).status, ExecutionStatus.SUCCEEDED, "record read back")
    values = result_of(record)
    return (
        f"SUCCEEDED at epoch {record.project_epoch}, progress 100, finished_at set, "
        f"layer_count={values['layer_count']}, JSON round trip ok"
    )


def at_04_submit_layer_add(ctx: Context) -> str:
    layers_before = len(project_layers())
    submitted = ctx.submit(
        "layer.add",
        {
            "kind": "vector",
            "source_path": str(ctx.fixtures.spaced),
            "name": "at04 spaced cities",
        },
    )
    equal(submitted.status, ExecutionStatus.QUEUED, "status right after submit")
    require(submitted.result is None, "a queued record carries no result")
    statuses = ctx.trace(submitted.execution_id)
    record = ctx.record(submitted.execution_id)
    equal(record.status, ExecutionStatus.SUCCEEDED, "layer.add after the event loop")
    layer_id = result_of(record)["layer_id"]
    require(layer_id in project_layers(), "the layer is not registered")
    equal(len(project_layers()), layers_before + 1, "layer count after the add")
    return (
        f"{' -> '.join(statuses)}; a layer loaded from a path with spaces "
        f"({ctx.fixtures.spaced.name}); project layers {layers_before} -> {layers_before + 1}"
    )


def at_06_submit_field_sum(ctx: Context) -> str:
    layer_id = cities_layer(ctx, "at06 cities")
    watch = ctx.watch_tasks()
    submitted = ctx.submit("layer.sum_numeric_field", {"layer_id": layer_id, "field_name": "pop"})
    equal(submitted.status, ExecutionStatus.QUEUED, "status right after submit")
    equal(submitted.strategy, ExecutionStrategy.BACKGROUND, "strategy of the record")

    pump_until(
        ctx.app,
        lambda: bool(watch.added) or ctx.record(submitted.execution_id).finished_at is not None,
        what="a real QgsTask to be submitted",
    )
    during = ctx.record(submitted.execution_id)
    require(0 <= during.progress <= 100, f"progress {during.progress} is outside 0-100")

    record = ctx.wait_terminal(submitted.execution_id)
    equal(record.status, ExecutionStatus.SUCCEEDED, "the field sum after the event loop")
    equal(record.progress, 100, "progress of the finished sum")
    require(bool(watch.added), "no QgsTask was ever submitted")
    pump_until(
        ctx.app,
        lambda: bool(watch.deleted),
        timeout=10.0,
        what="QGIS to delete the finished task",
    )
    equal(watch.deleted, watch.added, "the tasks QGIS deleted after they ended")
    require(not ctx.runner._active, "the adapter kept the finished submission")

    values = result_of(record)
    equal(values["total"], fixtures.POP_TOTAL, "the sum of pop")
    equal(values["count"], fixtures.POP_COUNT, "the number of summed values")
    equal(values["skipped_null"], fixtures.POP_NULLS, "the number of null values skipped")
    equal(values["snapshot_project_epoch"], ctx.lifecycle.current_epoch, "the snapshot epoch")
    return (
        f"QgsTask added (id {watch.added[0]}) and deleted by QGIS (id {watch.deleted[0]}), "
        f"status {record.status}, progress readable ({during.progress} while live, 100 at the "
        f"end); pop sum {values['total']} over {values['count']} values, "
        f"{values['skipped_null']} nulls — matches the baseline"
    )


def at_05_run_sync_refuses_background(ctx: Context) -> str:
    layer_id = cities_layer(ctx, "at05 cities")
    records_before = all_record_ids(ctx)
    tasks_before = manager_task_count(ctx)

    code = refused(
        lambda: ctx.run("layer.sum_numeric_field", {"layer_id": layer_id, "field_name": "pop"})
    )

    equal(code, UNSUPPORTED_EXECUTION_MODE, "the refusal code")
    equal(all_record_ids(ctx), records_before, "records created by the refused call")
    equal(manager_task_count(ctx), tasks_before, "QgsTasks created by the refused call")
    return (
        f"{code} raised; no execution record was created and no QgsTask was submitted "
        f"({tasks_before} tasks before and after)"
    )


def at_07_worker_exception(ctx: Context) -> str:
    ctx.register_probes()
    submitted = ctx.submit("probe.boom")
    record = ctx.wait_terminal(submitted.execution_id)
    equal(record.status, ExecutionStatus.FAILED, "a worker that raises")
    equal(error_code(record), INTERNAL_ERROR, "the code for a worker that raises")
    require(record.result is None, "a failed record carries no result")
    equal(ctx.engine._store.ids(LIVE_STATUSES), [], "records left in a live state")
    return (
        "the worker raised; the record is FAILED(INTERNAL_ERROR) with no result, no record "
        "stays RUNNING, and the process kept answering"
    )


def at_08_cancel_running(ctx: Context) -> str:
    ctx.register_probes()
    execution_id = ctx.submit(
        "probe.slow", {"label": "at08", "steps": 500, "step_seconds": 0.01}
    ).execution_id
    pump_until(
        ctx.app,
        lambda: ctx.status(execution_id) is ExecutionStatus.RUNNING,
        what="the task to report progress",
    )
    progress = ctx.record(execution_id).progress
    require(0 < progress < 100, f"progress before the cancel was {progress}")

    receipt = ctx.engine.cancel(execution_id)
    require(receipt.accepted, "the cancel request was refused")
    equal(receipt.status, ExecutionStatus.CANCEL_REQUESTED, "status right after the request")
    equal(
        ctx.status(execution_id),
        ExecutionStatus.CANCEL_REQUESTED,
        "the record does not announce an end it has not seen",
    )

    record = ctx.wait_terminal(execution_id)
    equal(record.status, ExecutionStatus.CANCELLED, "the final status")
    require(record.result is None and record.error is None, "a cancelled record carries neither")
    require("at08" not in PROBE_LOG.finalizes, "a cancelled execution was finalised")
    return (
        f"RUNNING (progress {progress}) -> CANCEL_REQUESTED (accepted) -> CANCELLED; "
        "no result applied, finalize_main never ran"
    )


def at_08b_cancel_scheduled(ctx: Context) -> str:
    ctx.register_probes()
    gate = threading.Event()
    GATES["at08b"] = gate
    submitted = ctx.submit(
        "probe.slow",
        {
            "label": "at08b",
            "steps": 50,
            "step_seconds": 0.01,
            "report_progress": False,
            "gate": "at08b",
        },
    )
    execution_id = submitted.execution_id
    try:
        pump_until(
            ctx.app,
            lambda: ctx.status(execution_id) is ExecutionStatus.SCHEDULED,
            what="the task to reach SCHEDULED",
        )
        pump(ctx.app, 0.15)
        equal(
            ctx.status(execution_id),
            ExecutionStatus.SCHEDULED,
            "the record while QGIS holds the task and it has reported no progress",
        )
        submission = ctx.runner._active.get(execution_id)
        require(submission is not None, "the adapter does not hold the submission")
        task = ctx.manager.task(submission.task_id)
        require(task is not None, "the QgsTask is not in this plugin's manager")
        require(not task.isCanceled(), "the real task was already cancelled")
        receipt = ctx.engine.cancel(execution_id)
        require(receipt.accepted, "the cancel of a scheduled task was refused")
        equal(ctx.status(execution_id), ExecutionStatus.CANCEL_REQUESTED, "status after the cancel")
        require(task.isCanceled(), "the cancel never reached the real QgsTask")
        pump(ctx.app, 0.1)
        equal(
            ctx.status(execution_id),
            ExecutionStatus.CANCEL_REQUESTED,
            "the record before the task itself reports",
        )
    finally:
        gate.set()

    record = ctx.wait_terminal(execution_id)
    equal(record.status, ExecutionStatus.CANCELLED, "the final status")
    require(record.result is None, "a cancelled task delivered a result")
    require("at08b" not in PROBE_LOG.finalizes, "a cancelled execution was finalised")
    return (
        "SCHEDULED (the record, while the real QgsTask was held at the probe's entry gate and "
        "reported no progress; QgsTask.cancel() then reached that task, isCanceled false -> "
        "true) -> CANCEL_REQUESTED (staying there until QGIS reported) -> CANCELLED. Scope: "
        "run() had already started on a worker thread, so the cancel-before-run() path inside "
        "the C++ task is not covered (see the report, deviation 3)"
    )


def at_08c_concurrency_limit(ctx: Context) -> str:
    ctx.register_probes()
    limit = ctx.engine._settings.max_active_background
    equal(limit, 2, "max_active_background (PRD 10.1 default)")
    PROBE_LOG.prepared.clear()
    PROBE_LOG.starts.clear()
    labels = ["at08c-a", "at08c-b", "at08c-c", "at08c-d"]
    ids = [
        ctx.submit("probe.slow", {"label": label, "steps": 10, "step_seconds": 0.03}).execution_id
        for label in labels
    ]
    active = lambda: ctx.engine._store.ids(  # noqa: E731 - one expression, used twice
        [ExecutionStatus.SCHEDULED, ExecutionStatus.RUNNING]
    )
    pump_until(ctx.app, lambda: len(active()) == 2, what="two active background tasks")
    queued = [ctx.status(execution_id) for execution_id in ids].count(ExecutionStatus.QUEUED)
    equal(queued, 2, "the calls waiting while the limit is full")

    peak = 0

    def tick() -> bool:
        nonlocal peak
        peak = max(peak, len(active()))
        return all(ctx.status(execution_id) in TERMINAL_STATUSES for execution_id in ids)

    pump_until(ctx.app, tick, timeout=30.0, what="all four tasks to finish")
    equal(peak, limit, "the highest number of tasks active at once")
    # 空位出现后按提交先后顺序启动下一个调用：快照在主线程的 prepare_main 里生成，顺序由适配器
    # 决定；worker 里 run() 的顺序属于 QThreadPool，不在这条约定之内。
    # A freed slot starts the next call in submission order: the snapshot is built in prepare_main
    # on the main thread, where the adapter decides the order; the worker's run() order belongs to
    # QThreadPool and is not part of that contract.
    equal(PROBE_LOG.prepared, labels, "the order the calls were granted a slot in")
    equal(
        sorted(PROBE_LOG.starts),
        sorted(labels),
        "the calls whose worker really ran (any thread order)",
    )
    equal(
        [ctx.status(execution_id) for execution_id in ids],
        [ExecutionStatus.SUCCEEDED] * len(ids),
        "the final status of every task",
    )

    # 一次提交六个调用：四个在等槽，每次释放都必须启动下一个，否则空位就丢了；六条记录全部
    # SUCCEEDED，才证明没有调用留在队列里。
    # Six calls at once mean four of them wait, and every release has to start the next call or the
    # slot is lost; all six reaching SUCCEEDED is what proves none stayed queued.
    PROBE_LOG.prepared.clear()
    wave = [f"at08c-{letter}" for letter in "efghij"]
    wave_ids = [
        ctx.submit("probe.slow", {"label": label, "steps": 2, "step_seconds": 0.01}).execution_id
        for label in wave
    ]
    pump_until(
        ctx.app,
        lambda: all(ctx.status(execution_id) in TERMINAL_STATUSES for execution_id in wave_ids),
        timeout=30.0,
        what="the six further background calls to finish",
    )
    equal(PROBE_LOG.prepared, wave, "the order the second wave was granted a slot in")
    equal(
        [ctx.status(execution_id) for execution_id in wave_ids],
        [ExecutionStatus.SUCCEEDED] * len(wave_ids),
        "the final status of every call in the second wave",
    )
    return (
        f"four submissions, never more than {peak} active, two QUEUED, PREPARING granted in "
        f"submission order ({', '.join(labels)}), all four workers ran; then six at once, "
        f"four of them waiting, and all six reached SUCCEEDED in submission order"
    )


def at_09_cancel_queued(ctx: Context) -> str:
    layers_before = len(project_layers())
    source = str(ctx.fixtures.spaced)
    first = ctx.submit("layer.add", {"kind": "vector", "source_path": source, "name": "at09 first"})
    second = ctx.submit(
        "layer.add", {"kind": "vector", "source_path": source, "name": "at09 second"}
    )
    equal(second.status, ExecutionStatus.QUEUED, "the second call right after submit")

    receipt = ctx.engine.cancel(second.execution_id)
    require(receipt.accepted, "the cancel of a queued call was refused")
    equal(receipt.status, ExecutionStatus.CANCELLED, "a queued call is cancelled at once")
    equal(ctx.status(second.execution_id), ExecutionStatus.CANCELLED, "status after the cancel")

    equal(
        ctx.wait_terminal(first.execution_id).status,
        ExecutionStatus.SUCCEEDED,
        "the call that was not cancelled",
    )
    pump(ctx.app, 0.1)
    equal(ctx.status(second.execution_id), ExecutionStatus.CANCELLED, "the cancelled call stayed")
    names = [layer.name() for layer in project_layers().values()]
    require("at09 second" not in names, "the cancelled call added its layer anyway")
    equal(len(project_layers()), layers_before + 1, "the layers the two calls added")
    return (
        "the queued call was CANCELLED at once and its Qt callback never ran the operation; "
        "only the other call added a layer"
    )


def at_10_cancel_finalizing(ctx: Context) -> str:
    ctx.register_probes()
    submitted = ctx.submit("probe.finalize_cancel")
    FINALIZE_CANCEL["engine"] = ctx.engine
    FINALIZE_CANCEL["execution_id"] = submitted.execution_id
    FINALIZE_CANCEL.pop("receipt", None)

    record = ctx.wait_terminal(submitted.execution_id)
    receipt: Any = FINALIZE_CANCEL.get("receipt")
    if receipt is None:
        raise AssertionError("the execution never reached finalize_main")
    equal(receipt.accepted, False, "a cancel inside FINALIZING was accepted")
    equal(receipt.reason, "FINALIZING", "the reason on the receipt")
    equal(receipt.status, ExecutionStatus.FINALIZING, "the status the cancel saw")
    equal(record.status, ExecutionStatus.SUCCEEDED, "the execution after the refused cancel")
    return (
        "cancel() called from finalize_main: accepted=false, reason=FINALIZING, status stayed "
        "FINALIZING; the execution then SUCCEEDED, so no illegal edge was taken"
    )


def at_11_project_replaced(ctx: Context) -> str:
    ctx.register_probes()
    cities_layer(ctx, "at11 cities")
    epoch_before = ctx.lifecycle.current_epoch
    execution_id = ctx.submit(
        "probe.slow", {"label": "at11", "steps": 500, "step_seconds": 0.01}
    ).execution_id
    pump_until(
        ctx.app,
        lambda: ctx.status(execution_id) is ExecutionStatus.RUNNING,
        what="the background task to run",
    )

    created = ctx.run("project.create", {"discard_current": True})
    equal(created.status, ExecutionStatus.SUCCEEDED, "project.create while a task ran")
    equal(ctx.lifecycle.current_epoch, epoch_before + 1, "the project epoch after the replacement")

    record = ctx.wait_terminal(execution_id)
    require(
        record.status in (ExecutionStatus.CANCELLED, ExecutionStatus.FAILED),
        f"the old task ended {record.status}",
    )
    if record.status is ExecutionStatus.FAILED:
        equal(error_code(record), STALE_PROJECT, "the code for a result from the old project")
    require(record.result is None, "a result from the old project was applied")
    equal(len(project_layers()), 0, "the layers in the new project")
    return (
        f"epoch {epoch_before} -> {epoch_before + 1}; the old task ended {record.status} with no "
        "result applied; the new project has no layers"
    )


def at_12_layer_removed_while_running(ctx: Context) -> str:
    from qgis.core import QgsProject

    ctx.register_probes()
    reset_project(ctx)
    layer_id = cities_layer(ctx, "at12 cities")
    request = SumNumericFieldParams(layer_id=layer_id, field_name="pop")

    equal(
        LayerSumNumericField().dependent_layer_ids(request), [layer_id], "the declared dependency"
    )
    resolved = _dependent_layers(ctx.engine, LayerSumNumericField(), request)
    equal([layer.id() for layer in resolved], [layer_id], "the dependency the adapter resolves")

    execution_id = ctx.submit(
        "probe.slow_sum", {"layer_id": layer_id, "field_name": "pop"}
    ).execution_id
    pump_until(
        ctx.app,
        lambda: ctx.status(execution_id) is ExecutionStatus.RUNNING,
        what="the slowed field sum to run",
    )
    submission = ctx.runner._active.get(execution_id)
    require(submission is not None, "the adapter does not hold the submission")
    task = ctx.manager.task(submission.task_id)
    require(task is not None, "the QgsTask is not in this plugin's manager")
    equal(
        [layer.id() for layer in task.dependentLayers()],
        [layer_id],
        "the layers on the real QgsTask",
    )

    QgsProject.instance().removeMapLayer(layer_id)
    record = ctx.wait_terminal(execution_id)
    equal(record.status, ExecutionStatus.CANCELLED, "the dependent task after its layer went away")
    require(record.result is None, "a cancelled task delivered a result")
    require(ctx.runner._active.get(execution_id) is None, "the adapter kept the submission")
    require(layer_id not in project_layers(), "the layer is still registered")
    return (
        "the sum names its layer, the adapter sets it on the real QgsTask, and removing that "
        "layer while the task ran ended it as CANCELLED with no result and no SIP error"
    )


def at_13_unload_with_running_worker(ctx: Context) -> str:
    ctx.register_probes()
    PROBE_LOG.finalizes.clear()
    layers = [layer.name() for layer in project_layers().values()]
    execution_id = ctx.submit(
        "probe.slow", {"label": "at13", "steps": 5000, "step_seconds": 0.01}
    ).execution_id
    pump_until(
        ctx.app,
        lambda: ctx.status(execution_id) is ExecutionStatus.RUNNING,
        what="the background task to run",
    )

    engine, supervisor = ctx.engine, ctx.plugin.supervisor
    ctx.plugin.unload()
    ctx.plugin = None

    refused = None
    try:
        engine.run_sync("project.get_info", {})
    except FrameworkError as error:
        refused = error.code
    else:
        raise AssertionError("the unloaded engine accepted new work")
    equal(refused, ENGINE_CLOSING, "the code for work offered after unload")
    require(
        not hasattr(ctx.iface, "agent_engine") and not hasattr(ctx.iface, "agent_registry"),
        "a console handle survived the unload",
    )
    record = engine.get(execution_id)
    equal(
        record.status,
        ExecutionStatus.CANCEL_REQUESTED,
        "the task was asked to stop, not declared stopped",
    )
    require(supervisor.live != frozenset(), "the supervisor let go while a task was alive")
    require(not qt_deleted(supervisor), "the supervisor was deleted before the task ended")

    pump_until(
        ctx.app,
        lambda: engine.get(execution_id).status in TERMINAL_STATUSES,
        timeout=60.0,
        what="the task to stop",
    )
    record = engine.get(execution_id)
    equal(record.status, ExecutionStatus.CANCELLED, "the task unloaded while it ran")
    require(record.result is None, "a result was applied after the unload")
    require("at13" not in PROBE_LOG.finalizes, "finalize_main ran after the unload")
    equal(
        [layer.name() for layer in project_layers().values()],
        layers,
        "the project after the unload",
    )

    pump_until(ctx.app, lambda: supervisor._disposed, timeout=30.0, what="the supervisor to let go")
    flush_deferred(ctx.app)
    require(qt_deleted(supervisor), "the supervisor's Qt object was never deleted")
    return (
        "unload while RUNNING: new work refused (ENGINE_CLOSING), the record went "
        "CANCEL_REQUESTED -> CANCELLED, no finalize ran, the project was not touched, and the "
        "supervisor was released and deleted only after the last task had ended"
    )


def at_14_submission_refused(ctx: Context) -> str:
    class Signal:
        def connect(self, callback: Any) -> None:
            self.callback = callback

    class RefusingManager:
        """模拟一个拒绝任务的加载器：``addTask`` 返回 0。

        A task manager that refuses the task: ``addTask`` returns 0.
        """

        progressChanged = Signal()

        def task(self, task_id: int) -> None:
            return None

        def addTask(self, task: Any) -> int:
            return 0

    layer_id = cities_layer(ctx, "at14 cities")
    registry = OperationRegistry()
    register_all(registry)
    runner = OperationTaskRunner(task_manager=RefusingManager())
    engine = OperationEngine(
        registry,
        Settings(),
        ProjectLifecycle(),
        iface=ctx.iface,
        project=ctx.engine._project,
        background_runner=runner,
    )
    submitted = engine.submit(
        "layer.sum_numeric_field", {"layer_id": layer_id, "field_name": "pop"}
    )
    pump_until(
        ctx.app,
        lambda: engine.get(submitted.execution_id).status in TERMINAL_STATUSES,
        what="the refused submission",
    )
    record = engine.get(submitted.execution_id)
    equal(record.status, ExecutionStatus.FAILED, "a task manager that refuses the task")
    equal(error_code(record), TASK_SUBMISSION_FAILED, "the code for a refused submission")
    equal(engine._store.ids(LIVE_STATUSES), [], "records left dangling")
    equal(len(engine._store.ids(ALL_STATUSES)), 1, "records in the simulated session")
    return (
        "a manager whose addTask() returns 0 (the simulated case of PRD 12.2): "
        "FAILED(TASK_SUBMISSION_FAILED), one record and none left dangling"
    )


def at_15_stateful_replace_refused(ctx: Context) -> str:
    reset_project(ctx)
    cities_layer(ctx, "at15 cities")
    layers_before = [(layer_id, layer.name()) for layer_id, layer in project_layers().items()]

    record = ctx.run("project.create", {})
    equal(record.status, ExecutionStatus.FAILED, "project.create without discard_current")
    equal(error_code(record), PROJECT_REPLACE_REQUIRED, "the code for an unarmed replacement")
    equal(
        [(layer_id, layer.name()) for layer_id, layer in project_layers().items()],
        layers_before,
        "the project after the refused call",
    )
    return (
        f"FAILED(PROJECT_REPLACE_REQUIRED) with {len(layers_before)} layer(s) still registered: "
        "the stateful project was left alone"
    )


def at_16_stateful_replace(ctx: Context) -> str:
    reset_project(ctx)
    cities_layer(ctx, "at16 cities")
    epoch_before = ctx.lifecycle.current_epoch
    equal(len(project_layers()), 1, "the stateful project before the replacement")

    record = ctx.run("project.create", {"discard_current": True, "crs_authid": "EPSG:3857"})
    equal(record.status, ExecutionStatus.SUCCEEDED, "an armed project.create")
    equal(result_of(record)["layer_count"], 0, "the layer count in the result")
    equal(len(project_layers()), 0, "the registered layers after the replacement")
    equal(result_of(record)["crs_authid"], "EPSG:3857", "the CRS of the new project")
    equal(ctx.lifecycle.current_epoch, epoch_before + 1, "the project epoch")
    return (
        f"SUCCEEDED: layer_count 1 -> 0, CRS EPSG:3857, epoch {epoch_before} -> {epoch_before + 1}"
    )


def at_17_save_refuses_existing(ctx: Context) -> str:
    target = ctx.fixtures.existing_project
    bytes_before = target.read_bytes()

    record = ctx.run("project.save", {"path": str(target)})
    equal(record.status, ExecutionStatus.FAILED, "project.save onto an existing file")
    equal(error_code(record), TARGET_EXISTS, "the code for an existing target")
    equal(target.read_bytes(), bytes_before, "the bytes of the existing target")

    fresh = ctx.fixtures.root / "written by at17.qgs"
    if fresh.exists():
        fresh.unlink()
    saved = ctx.run("project.save", {"path": str(fresh)})
    equal(saved.status, ExecutionStatus.SUCCEEDED, "project.save onto a new file")
    require(fresh.exists(), "no file was written")
    return (
        f"FAILED(TARGET_EXISTS) and the target's {len(bytes_before)} bytes are unchanged; the same "
        "save to a new path SUCCEEDED and wrote the file"
    )


def at_18_corrupt_project(ctx: Context) -> str:
    """同一条规则的两面：坏的 QGZ 被拒，好的被读进来。

    Both sides of the same rule: a corrupt QGZ is refused, a valid one is read.
    """

    record = ctx.run(
        "project.open",
        {"path": str(ctx.fixtures.corrupt_project), "discard_current": True},
    )
    equal(record.status, ExecutionStatus.FAILED, "project.open on a corrupt file")
    equal(error_code(record), PROJECT_OPEN_FAILED, "the code for a corrupt project")
    error = record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    require(
        "may have changed" in error.message,
        f"the message does not warn about the project state: {error.message!r}",
    )

    valid = ctx.run("project.open", {"path": str(ctx.fixtures.project), "discard_current": True})
    equal(valid.status, ExecutionStatus.SUCCEEDED, "project.open on a valid QGZ")
    values = result_of(valid)
    equal(values["layer_count"], 2, "the layers the valid QGZ holds")
    equal(values["invalid_layer_count"], 0, "the invalid layers of the valid QGZ")
    listed = result_of(ctx.run("layer.list"))["layers"]
    equal(
        {row["name"]: row["kind"] for row in listed},
        {"cities": "vector", "cities_tif": "raster"},
        "the layers the QGZ really loaded: the GeoPackage and the GeoTIFF",
    )
    return (
        f"corrupt QGZ -> FAILED(PROJECT_OPEN_FAILED) with the warning {error.message!r}; the valid "
        "QGZ -> SUCCEEDED with 2 layers, 0 invalid, one vector from the GeoPackage and one "
        "raster from the GeoTIFF; the session kept answering"
    )


def at_18b_project_with_unavailable_layer(ctx: Context) -> str:
    record = ctx.run("project.open", {"path": str(ctx.fixtures.missing), "discard_current": True})
    equal(
        record.status, ExecutionStatus.SUCCEEDED, "project.open on a project whose source is gone"
    )
    values = result_of(record)
    equal(values["layer_count"], 1, "the layers the project registers")
    equal(values["invalid_layer_count"], 1, "the layers that do not load")
    require(values["invalid_layer_count"] > 0, "the result claims every data source is valid")
    return (
        "read() succeeded with 1 registered layer and invalid_layer_count=1 (its data source was "
        "deleted), so the result does not claim every source is usable"
    )


def at_19_path_policy(ctx: Context) -> str:
    """路径只剩一条规则：必须是绝对路径；其余由 QGIS 与 provider 回答。

    One rule is left: a path has to be absolute; QGIS and the provider answer the rest.
    """

    reset_project(ctx)
    outside = ctx.fixtures.outside / "at19"
    outside.mkdir(exist_ok=True)

    relative = ctx.run(
        "layer.add", {"kind": "vector", "source_path": "cities.gpkg", "name": "at19"}
    )
    equal(relative.status, ExecutionStatus.FAILED, "layer.add from a relative path")
    equal(error_code(relative), PATH_NOT_ALLOWED, "the code for a relative path")

    saved = ctx.run("project.save", {"path": str(outside / "at19.qgs")})
    equal(saved.status, ExecutionStatus.SUCCEEDED, "project.save outside any configured root")
    require((outside / "at19.qgs").exists(), "the file the save reported was not written")

    # 一个 URL 不是 Windows 的绝对路径，所以它和相对路径落在同一条规则上。
    # A URL is not a Windows absolute path, so it meets the same rule as a relative path.
    remote = ctx.run(
        "layer.add",
        {"kind": "vector", "source_path": "https://127.0.0.1:9/x.gpkg", "name": "at19 remote"},
    )
    equal(remote.status, ExecutionStatus.FAILED, "layer.add from an https URI")
    equal(error_code(remote), PATH_NOT_ALLOWED, "the code for an https URI")
    reset_project(ctx)
    return (
        "a relative path -> FAILED(PATH_NOT_ALLOWED); project.save into a directory outside any "
        "configured root -> SUCCEEDED and the file exists; an https URI -> PATH_NOT_ALLOWED, the "
        "same absolute-path rule rather than a scheme rule"
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
    reset_project(ctx)
    layer_id = ctx.add_layer(ctx.fixtures.cities, "at21 not in the tree", add_to_legend=False)
    rows = result_of(ctx.run("layer.list"))["layers"]
    row = next(item for item in rows if item["layer_id"] == layer_id)
    equal(row["in_layer_tree"], False, "in_layer_tree")
    equal(row["visible"], None, "visible")
    equal(row["effective_visible"], None, "effective_visible")
    return (
        "a registered layer with no layer tree node lists as in_layer_tree=false, "
        "visible=null and effective_visible=null"
    )


def at_21b_group_hides_layer(ctx: Context) -> str:
    from qgis.core import QgsProject

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
    equal(result_of(record)["changed"], False, "the layer was already checked")
    equal(group.itemVisibilityChecked(), False, "the parent group's check state")
    return (
        "visible=true while effective_visible=false under an unchecked group; "
        "set_visibility(true) reported changed=false and left the group unchecked"
    )


def at_22_visibility_idempotent(ctx: Context) -> str:
    reset_project(ctx)
    layer_id = cities_layer(ctx, "at22 cities")
    first = ctx.run("layer.set_visibility", {"layer_id": layer_id, "visible": False})
    equal(first.status, ExecutionStatus.SUCCEEDED, "the first set_visibility")
    equal(result_of(first)["changed"], True, "the first call changed the state")

    second = ctx.run("layer.set_visibility", {"layer_id": layer_id, "visible": False})
    equal(second.status, ExecutionStatus.SUCCEEDED, "the repeated set_visibility")
    equal(result_of(second)["changed"], False, "the repeated call")
    equal(result_of(second)["visible"], False, "the layer after the repeated call")
    return (
        "the same set_visibility twice: SUCCEEDED with changed=true, then SUCCEEDED with "
        "changed=false"
    )


def at_23_finalize_or_output_fails(ctx: Context) -> str:
    ctx.register_probes()
    raising = ctx.wait_terminal(ctx.submit("probe.bad_finalize").execution_id)
    equal(raising.status, ExecutionStatus.FAILED, "a finalize_main that raises")
    equal(error_code(raising), INTERNAL_ERROR, "the code for a raising finalize_main")

    refused = ctx.wait_terminal(ctx.submit("probe.bad_output").execution_id)
    equal(refused.status, ExecutionStatus.FAILED, "an output the declared model refuses")
    equal(error_code(refused), OUTPUT_VALIDATION_FAILED, "the code for a refused output")
    require(refused.result is None, "a failed record carries a result")
    return (
        "a raising finalize_main -> FAILED(INTERNAL_ERROR) and an output outside the model -> "
        "FAILED(OUTPUT_VALIDATION_FAILED); neither was marked SUCCEEDED"
    )


def at_24_retention(ctx: Context) -> str:
    ctx.register_probes()
    equal(ctx.engine._settings.max_terminal_records, 200, "max_terminal_records (PRD 10.1 default)")
    equal(
        ctx.engine._settings.terminal_ttl_seconds, 1800, "terminal_ttl_seconds (PRD 10.1 default)"
    )

    gate = threading.Event()
    GATES["at24"] = gate
    slow = ctx.submit(
        "probe.slow",
        {
            "label": "at24",
            "steps": 5,
            "step_seconds": 0.01,
            "report_progress": False,
            "gate": "at24",
        },
    )
    try:
        pump_until(
            ctx.app,
            lambda: ctx.status(slow.execution_id) is ExecutionStatus.SCHEDULED,
            what="the live record the history must not evict",
        )
        terminal_before = ctx.engine._store.ids(TERMINAL_STATUSES)
        oldest = terminal_before[0]
        for _ in range(200):
            ctx.run("project.get_info")
        terminal_after = ctx.engine._store.ids(TERMINAL_STATUSES)
        equal(len(terminal_after), 200, "the number of terminal records kept")
        require(oldest not in terminal_after, "the oldest terminal record was not evicted")
        evicted = refused(lambda: ctx.engine.get(oldest))
        equal(evicted, EXECUTION_NOT_FOUND, "the code for an evicted id")
        equal(
            ctx.status(slow.execution_id),
            ExecutionStatus.SCHEDULED,
            "the live record while the history was pruned",
        )
    finally:
        gate.set()
    ctx.wait_terminal(slow.execution_id)

    # 过期的那一半需要 1800 秒保留时间，所以用注入的时钟对同一个 store 跑。
    # The TTL half needs 1800 seconds of retention time, so it runs on an injected clock against the
    # same store.
    now = [1000.0]
    registry = OperationRegistry()
    register_all(registry)
    engine = OperationEngine(
        registry,
        Settings(),
        ProjectLifecycle(),
        iface=ctx.iface,
        project=ctx.engine._project,
        clock=lambda: now[0],
    )
    kept = engine.run_sync("project.get_info", {})
    now[0] += 1799
    equal(
        engine.get(kept.execution_id).status, ExecutionStatus.SUCCEEDED, "a record inside the TTL"
    )
    now[0] += 2
    expired = refused(lambda: engine.get(kept.execution_id))
    equal(expired, EXECUTION_NOT_FOUND, "the code for a record past its TTL")
    return (
        "200 terminal records kept, the oldest evicted id returns EXECUTION_NOT_FOUND, the live "
        "record survived the pruning; on an injected clock a record 1799 s old is readable and "
        "1801 s old returns EXECUTION_NOT_FOUND"
    )


def at_25_three_sessions(ctx: Context) -> str:
    ctx.unload()
    lifecycles = []
    registries = []
    engines = []
    for number in (1, 2, 3):
        plugin = ctx.load()
        lifecycles.append(plugin.lifecycle)
        registries.append(plugin.registry)
        engines.append(plugin.engine)
        equal(len(plugin.registry.list_specs()), 17, f"round {number}: the seventeen operations")
        require(hasattr(ctx.iface, "agent_engine"), f"round {number}: no console handle")
        from qgis.core import QgsProject

        QgsProject.instance().clear()
        equal(plugin.lifecycle.current_epoch, 1, f"round {number}: one bump per project clear")
        supervisor = plugin.supervisor
        ctx.unload()
        require(
            not hasattr(ctx.iface, "agent_engine") and not hasattr(ctx.iface, "agent_registry"),
            f"round {number}: a console handle survived the unload",
        )
        require(supervisor._disposed, f"round {number}: the supervisor still holds the session")
        require(qt_deleted(supervisor), f"round {number}: the supervisor was not deleted")

    from qgis.core import QgsProject

    QgsProject.instance().clear()
    equal(
        [lifecycle.current_epoch for lifecycle in lifecycles],
        [1, 1, 1],
        "the epochs after a clear that every session is gone for",
    )
    equal(len({id(engine) for engine in engines}), 3, "a fresh engine per session")
    equal(len({id(registry) for registry in registries}), 3, "a fresh registry per session")
    for engine in engines:
        code = refused(partial(engine.run_sync, "project.get_info", {}))
        equal(code, ENGINE_CLOSING, "the code for work offered to an unloaded engine")
    return (
        "three loads and unloads: seventeen operations per round, exactly one epoch bump per "
        "project clear, no console handle left, a fresh engine and registry per round, and a "
        "clear after every session is gone left all three epochs at 1 (no repeated signal "
        "connection); each unloaded engine refuses new work"
    )


def at_26_wrong_thread(ctx: Context) -> str:
    cities_layer(ctx, "at26 cities")
    records_before = all_record_ids(ctx)
    layers_before = project_layer_ids()
    outcomes: dict[str, str] = {}

    def from_worker() -> None:
        calls = {
            "run_sync": lambda: ctx.engine.run_sync("project.get_info", {}),
            "submit": lambda: ctx.engine.submit("layer.list", {}),
            "get": lambda: ctx.engine.get("an-execution-id"),
            "cancel": lambda: ctx.engine.cancel("an-execution-id"),
        }
        for label, call in calls.items():
            try:
                call()
            except FrameworkError as error:
                outcomes[label] = error.code
            except Exception as error:  # noqa: BLE001 - the type is the evidence
                outcomes[label] = type(error).__name__
            else:
                outcomes[label] = "returned"

    worker = threading.Thread(target=from_worker, name="at26-worker")
    worker.start()
    worker.join(30)
    require(not worker.is_alive(), "the worker thread did not finish")

    equal(sorted(outcomes), ["cancel", "get", "run_sync", "submit"], "the calls the worker made")
    for label, code in outcomes.items():
        equal(code, WRONG_THREAD, f"{label} from a worker thread")
    equal(all_record_ids(ctx), records_before, "the records the worker created")
    equal(project_layer_ids(), layers_before, "the project after the worker's calls")
    return (
        "run_sync, submit, get and cancel called from a worker thread all raised WRONG_THREAD; no "
        "record was created and the project was not touched"
    )


def at_27_pydantic_v2(ctx: Context) -> str:
    specs = ctx.registry.list_specs()
    equal(len(specs), 17, "the seventeen specs")
    for spec in specs:
        exported = spec.model_dump_json()
        require(json.loads(exported) is not None, f"{spec.name}: the spec did not export JSON")
        equal(
            OperationSpec.model_validate_json(exported).name, spec.name, f"{spec.name}: round trip"
        )

    valid = LayerSetVisibilityParams.model_validate({"layer_id": "x", "visible": True})
    equal(valid.visible, True, "a valid input")
    rejected = None
    try:
        LayerSetVisibilityParams.model_validate({"layer_id": "x", "visible": "true"})
    except ValidationError as error:
        rejected = type(error).__name__
    else:
        raise AssertionError("pydantic accepted a string where a boolean belongs")
    equal(rejected, "ValidationError", "the refusal of an invalid input")

    record = ctx.run("project.get_info")
    exported = record.model_dump_json()
    require(json.loads(exported) is not None, "the record did not export JSON")
    equal(ExecutionRecord.model_validate_json(exported), record, "the record round trip")
    return (
        f"pydantic {pydantic.VERSION}: the seventeen specs export and round-trip JSON, "
        "StrictBool refuses the string 'true', and the terminal record with its result "
        "round-trips. The v1 half of AT-27 is a separate, skipped row"
    )


def at_27_v1_half(ctx: Context) -> str:
    raise Skip(
        "pydantic 1.x is not installed here and this build does not support it (charter "
        "decision 5: the DTOs are written against v2 and plugin.require_supported_pydantic "
        "refuses any other major), so the v1 half of AT-27 cannot be run"
    )


def at_28_layer_add_sub_layer(ctx: Context) -> str:
    """GeoPackage 里指定名字的一张表，经 OGR 编码器加进来。

    One named table in a GeoPackage, reached through the OGR encoder.
    """

    from qgis.core import QgsProject

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
    return (
        f"layer.add(sub_layer='{fixtures.ROADS_LAYER}') on {source.name!r} SUCCEEDED, provider "
        f"ogr, featureCount 1 (cities holds 5): the OGR encoder chose the named table"
    )


def at_29_open_accepts_a_source_outside_any_root(ctx: Context) -> str:
    """工程自己点名的数据源不再由插件审：打开成功，图层留着。

    The plugin no longer judges the sources a project names: the open succeeds and the layer stays.
    """

    reset_project(ctx)
    record = ctx.run("project.open", {"path": str(ctx.fixtures.leak), "discard_current": True})

    equal(
        record.status,
        ExecutionStatus.SUCCEEDED,
        "project.open on a project whose layer source is outside any configured root",
    )
    names = [layer.name() for layer in project_layers().values()]
    require("leaked" in names, f"the outside layer is missing: {names}")
    equal(len(project_layers()), 1, "the layers the project kept")
    reset_project(ctx)
    return (
        "a QGZ whose only layer names a GeoPackage in a sibling directory: SUCCEEDED with that "
        "layer still registered, because the plugin no longer judges a project's data sources"
    )


def at_30_open_refuses_a_remote_source_before_reading(ctx: Context) -> str:
    """工程文件不能在检查之前就花掉一次网络往返：先看文件自己的字节，再交给 read()。

    A project file may not cost a network round trip before it is checked.
    """

    from qgis.core import QgsProject

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at30 kept")
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()
    timings: list[str] = []
    for label, target in (
        ("a /vsicurl source", ctx.fixtures.remote_vsicurl),
        ("a UNC source", ctx.fixtures.remote_unc),
    ):
        started = time.monotonic()
        record = ctx.run("project.open", {"path": str(target), "discard_current": True})
        elapsed = time.monotonic() - started
        equal(record.status, ExecutionStatus.FAILED, f"project.open on {label}")
        equal(error_code(record), PATH_NOT_ALLOWED, f"the code for {label}")
        # 文件自己的 datasource 文本在 read() 之前就被拒，所以 HTTP 重试与 SMB 连接都不会发生在
        # 主线程上。
        # The file's own datasource text is refused before read(), so neither the HTTP retry nor the
        # SMB connect happens on the main thread.
        require(elapsed < 1.0, f"{label} took {elapsed:.2f}s: read() reached it")
        equal(project_layer_ids(), before, f"the project's layers after {label}")
        equal(QgsProject.instance().fileName(), file_before, f"the project's file after {label}")
        timings.append(f"{label} {elapsed * 1000:.0f} ms")
    reset_project(ctx)
    return (
        f"FAILED(PATH_NOT_ALLOWED) in {', '.join(timings)}; the project file was scanned "
        "before read(), so the old project and its one layer are still loaded"
    )


def at_31_open_refuses_an_encoded_or_spaced_datasource(ctx: Context) -> str:
    """只按 UTF-8 明文扫描可以被一个编码或一个空格绕过：UTF-16 与带空格的标签都测。

    A UTF-8-only text scan can be walked past with an encoding or a space.
    """

    from qgis.core import QgsProject

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at31 kept")
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()
    timings: list[str] = []
    for label, target, expected in (
        ("a UTF-16 project", ctx.fixtures.remote_utf16, "not UTF-8"),
        ("a <datasource > tag", ctx.fixtures.remote_spaced, "remote data source"),
    ):
        started = time.monotonic()
        record = ctx.run("project.open", {"path": str(target), "discard_current": True})
        elapsed = time.monotonic() - started
        equal(record.status, ExecutionStatus.FAILED, f"project.open on {label}")
        equal(error_code(record), PATH_NOT_ALLOWED, f"the code for {label}")
        error = record.error
        if error is None:
            raise AssertionError("a failed record carries an error")
        require(expected in error.message, f"the message for {label}: {error.message!r}")
        # 两种写法都不能走到 read()：扫描若看漏数据源，就要付 10 s 的 GDAL 重试（实测）。
        # Neither form may reach read(): a datasource the scan misses costs 10 s of GDAL retries.
        require(elapsed < 0.5, f"{label} took {elapsed:.2f}s: read() reached the source")
        equal(project_layer_ids(), before, f"the project's layers after {label}")
        equal(QgsProject.instance().fileName(), file_before, f"the project's file after {label}")
        timings.append(f"{label} {elapsed * 1000:.0f} ms")
    reset_project(ctx)
    return (
        f"FAILED(PATH_NOT_ALLOWED) in {', '.join(timings)}; the file was scanned in its own "
        "bytes, so the old project and its one layer are still loaded"
    )


def at_32_open_keeps_a_source_whose_directory_is_gone(ctx: Context) -> str:
    """数据源目录被搬走：工程照样打开，只是那一层加载不出来。

    A moved-away source directory: the project still opens, and only that layer fails to load.
    """

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at32 old")

    record = ctx.run(
        "project.open",
        {"path": str(ctx.fixtures.moved_directory), "discard_current": True},
    )

    equal(
        record.status,
        ExecutionStatus.SUCCEEDED,
        "project.open on a project whose source directory is gone",
    )
    values = result_of(record)
    equal(values["layer_count"], 1, "the layers the project registers")
    equal(values["invalid_layer_count"], 1, "the layers that do not load")
    listed = result_of(ctx.run("layer.list"))["layers"]
    equal([row["name"] for row in listed], ["moved"], "the layers still registered")
    reset_project(ctx)
    return (
        "SUCCEEDED with layer_count 1 and invalid_layer_count 1, the source directory "
        "does not exist: the layer stays registered and the read is not turned into "
        "PATH_NOT_ALLOWED"
    )


def at_33_open_refuses_an_oversized_member_before_unpacking_it(ctx: Context) -> str:
    """一个很小的 QGZ 可以装下解压后 300 MB 的成员，而且解压发生在主线程上。

    A tiny QGZ may hold a member that expands to 300 MB, on the main thread.
    """

    from qgis.core import QgsProject

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at33 kept")
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()
    on_disk = ctx.fixtures.oversized.stat().st_size

    started = time.monotonic()
    record = ctx.run("project.open", {"path": str(ctx.fixtures.oversized), "discard_current": True})
    elapsed = time.monotonic() - started

    equal(record.status, ExecutionStatus.FAILED, "project.open on an oversized member")
    equal(error_code(record), PATH_NOT_ALLOWED, "the code for an oversized member")
    error = record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    require("too large" in error.message, f"the message: {error.message!r}")
    require(elapsed < 1.0, f"the {on_disk} byte archive took {elapsed:.2f}s: its member was read")
    equal(project_layer_ids(), before, "the project's layers after the refusal")
    equal(QgsProject.instance().fileName(), file_before, "the project's file after the refusal")
    reset_project(ctx)
    return (
        f"a {on_disk} byte QGZ whose member declares 300 MB: FAILED(PATH_NOT_ALLOWED) in "
        f"{elapsed * 1000:.0f} ms, after at most the 16 MB budget was decompressed; the old "
        "project kept its layer"
    )


def at_34_unreadable_member_is_a_project_read_failure(ctx: Context) -> str:
    """扫描器解不开的 QGZ 是文件自己的问题，不该报成插件的内部错误。

    A QGZ the scanner cannot unpack is the file's problem, not the plugin's.
    """

    reset_project(ctx)

    record = ctx.run(
        "project.open",
        {"path": str(ctx.fixtures.encrypted), "discard_current": True},
    )

    equal(
        record.status,
        ExecutionStatus.FAILED,
        "project.open on a QGZ whose member is encrypted",
    )
    equal(
        error_code(record),
        PROJECT_OPEN_FAILED,
        "the code for a QGZ the scanner cannot unpack (INTERNAL_ERROR would blame the plugin)",
    )
    error = record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    require("may have changed" in error.message, f"the message: {error.message!r}")
    reset_project(ctx)
    return (
        "the member carries the zip encryption flag, so extracting it raises RuntimeError; "
        "read() is never given that archive (QGIS 3.44 crashes on it, measured) and the "
        "caller gets PROJECT_OPEN_FAILED with read()'s message, not INTERNAL_ERROR"
    )


def at_35_open_refuses_a_deeply_nested_project_before_reading_it(ctx: Context) -> str:
    """嵌套过深的工程会让 ``read()`` 的解析器崩溃，所以它绝不能走到 read()。

    A deeply nested project crashes ``read()``'s parser, so it never reaches it.
    """

    from qgis.core import QgsProject

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at35 kept")
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()

    started = time.monotonic()
    record = ctx.run(
        "project.open",
        {"path": str(ctx.fixtures.deep_project), "discard_current": True},
    )
    elapsed = time.monotonic() - started

    equal(record.status, ExecutionStatus.FAILED, "project.open on a deeply nested project")
    equal(error_code(record), PROJECT_OPEN_FAILED, "the code for a deeply nested project")
    error = record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    require("nests deeper" in error.message, f"the message: {error.message!r}")
    require(elapsed < 1.0, f"the 8000-level project took {elapsed:.2f}s")
    equal(project_layer_ids(), before, "the project's layers after the refusal")
    equal(QgsProject.instance().fileName(), file_before, "the project's file after the refusal")
    reset_project(ctx)
    return (
        "a 56 KB project nested 8000 levels deep: FAILED(PROJECT_OPEN_FAILED) in "
        f"{elapsed * 1000:.0f} ms, before read(); the process is still here and the old "
        "project kept its layer (QGIS 3.44 crashed with 0xC00000FD when read() got this "
        "file, measured)"
    )


def at_36_open_refuses_an_unclosed_datasource_project_in_milliseconds(ctx: Context) -> str:
    """984 KB 未闭合标签曾经让主线程耗掉 661 s（实测），现在是一次线性扫描。

    984 KB of unclosed tags once cost the main thread 661 s; the scan is linear now.
    """

    from qgis.core import QgsProject

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at36 kept")
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()
    on_disk = ctx.fixtures.unclosed_project.stat().st_size

    started = time.monotonic()
    record = ctx.run(
        "project.open",
        {"path": str(ctx.fixtures.unclosed_project), "discard_current": True},
    )
    elapsed = time.monotonic() - started

    equal(record.status, ExecutionStatus.FAILED, "project.open on 984 KB of unclosed tags")
    equal(error_code(record), PROJECT_OPEN_FAILED, "the code for 984 KB of unclosed tags")
    error = record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    require("nests deeper" in error.message, f"the message: {error.message!r}")
    require(elapsed < 0.5, f"the {on_disk} byte project took {elapsed:.2f}s to scan")
    equal(project_layer_ids(), before, "the project's layers after the refusal")
    equal(QgsProject.instance().fileName(), file_before, "the project's file after the refusal")
    reset_project(ctx)
    return (
        f"a {on_disk} byte project of 82000 unclosed <datasource> tags: "
        f"FAILED(PROJECT_OPEN_FAILED) in {elapsed * 1000:.0f} ms, before read(); the old "
        "regex was still scanning this file after 150 s, and the audit measured 661 s for "
        "this shape"
    )


def at_37_open_refuses_a_qgz_over_the_total_budget(ctx: Context) -> str:
    """预算算的是各成员之和，不是逐个成员单独卡：十七个 1 MB 成员单看不超，加起来超。

    The budget is the sum of the members, not each member on its own.
    """

    from qgis.core import QgsProject

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at37 kept")
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()
    on_disk = ctx.fixtures.many_members.stat().st_size

    started = time.monotonic()
    record = ctx.run(
        "project.open",
        {"path": str(ctx.fixtures.many_members), "discard_current": True},
    )
    elapsed = time.monotonic() - started

    equal(record.status, ExecutionStatus.FAILED, "project.open on an archive over the budget")
    equal(error_code(record), PATH_NOT_ALLOWED, "the code for an archive over the budget")
    error = record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    require("too large" in error.message, f"the message: {error.message!r}")
    require(elapsed < 1.0, f"the {on_disk} byte archive took {elapsed:.2f}s")
    equal(project_layer_ids(), before, "the project's layers after the refusal")
    equal(QgsProject.instance().fileName(), file_before, "the project's file after the refusal")
    reset_project(ctx)
    return (
        f"a {on_disk} byte QGZ with {fixtures.BUDGET_MEMBERS} one-megabyte .qgs members "
        f"(each under 16 MB, {fixtures.BUDGET_MEMBERS} MB together): "
        f"FAILED(PATH_NOT_ALLOWED) in {elapsed * 1000:.0f} ms, after at most the 16 MB budget "
        "was decompressed; the old per-member check accepted this shape and read every "
        "member (measured peak 200 MB for 100 members)"
    )


def at_38_open_refuses_a_tag_spam_project_in_milliseconds(ctx: Context) -> str:
    """16 MB 的 XML 元素靠计数拒绝，而不是逐个元素走一遍：一次 bytes.count 就够。

    16 MB of XML elements is refused by count, not walked element by element.
    """

    from qgis.core import QgsProject

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at38 kept")
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()
    on_disk = ctx.fixtures.tag_spam.stat().st_size

    started = time.monotonic()
    record = ctx.run(
        "project.open",
        {"path": str(ctx.fixtures.tag_spam), "discard_current": True},
    )
    elapsed = time.monotonic() - started

    equal(record.status, ExecutionStatus.FAILED, "project.open on a tag-spam project")
    equal(error_code(record), PROJECT_OPEN_FAILED, "the code for a tag-spam project")
    error = record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    require("XML tags" in error.message, f"the message: {error.message!r}")
    # 冷读 1.9 s、热读 234 ms（实测），所以这个上限区分的是“一次 bytes.count”与“逐元素解析”，
    # 而不是两次磁盘缓存状态的差异。
    # A cold read of this 16.7 MB file cost 1.9 s and a warm one 234 ms, so the bound separates one
    # bytes.count from a parse per element, not one disk cache state from another.
    require(elapsed < 5.0, f"the {on_disk} byte project took {elapsed:.2f}s")
    equal(project_layer_ids(), before, "the project's layers after the refusal")
    equal(QgsProject.instance().fileName(), file_before, "the project's file after the refusal")
    reset_project(ctx)
    return (
        f"a {on_disk} byte project of {fixtures.TAG_SPAM_PAIRS} empty elements: "
        f"FAILED(PROJECT_OPEN_FAILED) in {elapsed * 1000:.0f} ms, one bytes.count over "
        "the bytes; a parse per element would have held the main thread for seconds"
    )


def at_39_open_refuses_a_datasource_hidden_by_an_attribute(ctx: Context) -> str:
    """属性值里的 ``>`` 是合法 XML，不是标签的结束：在第一个 ``>`` 处截断就会看漏数据源。

    A ``>`` inside an attribute value is legal XML, not the end of the tag.
    """

    from qgis.core import QgsProject

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at39 kept")
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    target = ctx.fixtures.root / "attribute source.qgs"
    fixtures.write_attribute_remote(target, ctx.fixtures.raster, port)

    connections = 0
    started = time.monotonic()
    try:
        record = ctx.run("project.open", {"path": str(target), "discard_current": True})
        elapsed = time.monotonic() - started
        try:
            connection, _ = listener.accept()
        except TimeoutError:
            pass
        else:
            connections += 1
            connection.close()
    finally:
        listener.close()

    equal(record.status, ExecutionStatus.FAILED, "project.open on an attribute-hidden source")
    equal(error_code(record), PATH_NOT_ALLOWED, "the code for an attribute-hidden source")
    error = record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    require("remote data source" in error.message, f"the message: {error.message!r}")
    equal(connections, 0, "TCP connections made while the project was opened")
    require(elapsed < 1.0, f"the {target.stat().st_size} byte project took {elapsed:.2f}s")
    equal(project_layer_ids(), before, "the project's layers after the refusal")
    equal(QgsProject.instance().fileName(), file_before, "the project's file after the refusal")
    reset_project(ctx)
    return (
        '<datasource x="/>">/vsicurl/http://127.0.0.1:<a port this case listens on>: '
        f"FAILED(PATH_NOT_ALLOWED) in {elapsed * 1000:.0f} ms with {connections} TCP "
        "connections; the old scan ended the tag at the first >, accepted, and read() made 9 "
        "connections in 0.08 s (measured). The old project kept its layer"
    )


def at_40_open_reads_a_project_with_labelled_layers(ctx: Context) -> str:
    """属性里的 ``>`` 正是 QGIS 给每个标注写出来的东西，所以正常工程不能被拒。

    A ``>`` inside an attribute is what QGIS itself writes for every label.
    """

    reset_project(ctx)
    on_disk = ctx.fixtures.labelled.stat().st_size

    started = time.monotonic()
    record = ctx.run("project.open", {"path": str(ctx.fixtures.labelled), "discard_current": True})
    elapsed = time.monotonic() - started

    equal(
        record.status,
        ExecutionStatus.SUCCEEDED,
        "project.open on a project with labelled layers",
    )
    values = result_of(record)
    equal(
        values["layer_count"],
        fixtures.LABELLED_LAYERS,
        "the layers the labelled project holds",
    )
    equal(values["invalid_layer_count"], 0, "the invalid layers of the labelled project")
    # 墙钟时间只能区分“答出来了”与“主线程被停住”：同一个 30 层工程的读取两次整套运行分别实测
    # 1.1 s 与 2.1 s，而文件扫描两次都是 31 ms，所以再收紧上限测的就不是代码本身了。
    # Wall clock only separates "answered" from "stopped the main thread": the same 30-layer read
    # took 1.1 s and then 2.1 s in two full runs while the scan of the file took 31 ms both times,
    # so a tighter bound would not measure the code.
    require(elapsed < 20.0, f"the {on_disk} byte project took {elapsed:.2f}s")
    reset_project(ctx)
    return (
        f"a {on_disk} byte project with {fixtures.LABELLED_LAYERS} labelled layers: SUCCEEDED "
        f"with {values['layer_count']} layers in {elapsed * 1000:.0f} ms. The old counter "
        "drifted +2 per labelled layer and refused 28 of them (measured); the parser counts "
        "elements, and every label tag is one"
    )


def at_41_open_refuses_depth_written_inside_cdata_comments_or_attributes(
    ctx: Context,
) -> str:
    """三种把深度计数压住的写法：CDATA、注释、属性值里藏着标签的结束。

    The three shapes that held the old depth counter down.
    """

    from qgis.core import QgsProject

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at41 kept")
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()
    timings: list[str] = []
    for label, target in (
        ("CDATA", ctx.fixtures.hidden_depth_cdata),
        ("a comment", ctx.fixtures.hidden_depth_comment),
        ("an attribute", ctx.fixtures.hidden_depth_attribute),
    ):
        on_disk = target.stat().st_size
        started = time.monotonic()
        record = ctx.run("project.open", {"path": str(target), "discard_current": True})
        elapsed = time.monotonic() - started
        equal(record.status, ExecutionStatus.FAILED, f"project.open on depth hidden by {label}")
        equal(error_code(record), PROJECT_OPEN_FAILED, f"the code for {label}")
        error = record.error
        if error is None:
            raise AssertionError("a failed record carries an error")
        require("nests deeper" in error.message, f"the message for {label}: {error.message!r}")
        require(elapsed < 0.5, f"the {on_disk} byte project took {elapsed:.2f}s")
        equal(project_layer_ids(), before, f"the project's layers after {label}")
        equal(QgsProject.instance().fileName(), file_before, f"the project's file after {label}")
        timings.append(f"{label} {on_disk} bytes {elapsed * 1000:.0f} ms")
    reset_project(ctx)
    return (
        f"{'; '.join(timings)}: FAILED(PROJECT_OPEN_FAILED) with 'nests deeper' before read(). "
        "The audit measured read() exit code -1073741571 (0xC00000FD) on all three; this "
        "process is still answering and the old project kept its layer"
    )


def at_42_open_refuses_an_archive_whose_sibling_member_is_encrypted(ctx: Context) -> str:
    """``read()`` 会把归档里每个成员都解出来，不只 ``.qgs`` 那个。

    ``read()`` unpacks every member, not only the ``.qgs`` one.
    """

    from qgis.core import QgsProject

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at42 kept")
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()
    on_disk = ctx.fixtures.encrypted_sibling.stat().st_size

    started = time.monotonic()
    record = ctx.run(
        "project.open",
        {"path": str(ctx.fixtures.encrypted_sibling), "discard_current": True},
    )
    elapsed = time.monotonic() - started

    equal(record.status, ExecutionStatus.FAILED, "project.open on an encrypted sibling member")
    equal(error_code(record), PROJECT_OPEN_FAILED, "the code for an encrypted sibling member")
    error = record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    require("may have changed" in error.message, f"the message: {error.message!r}")
    require(elapsed < 0.5, f"the {on_disk} byte archive took {elapsed:.2f}s")
    equal(project_layer_ids(), before, "the project's layers after the refusal")
    equal(QgsProject.instance().fileName(), file_before, "the project's file after the refusal")
    reset_project(ctx)
    return (
        f"a {on_disk} byte QGZ whose extra.db carries the encryption flag: "
        f"FAILED(PROJECT_OPEN_FAILED) in {elapsed * 1000:.0f} ms, before read(). The audit "
        "measured read() exit code -1073741819 (0xC0000005) on this shape; this process is "
        "still answering and the old project kept its layer"
    )


def at_43_open_reads_a_project_with_a_deep_group_tree(ctx: Context) -> str:
    """真实的分组树远远到不了的深度上限，所以正常工程不能被它拒掉。

    A depth limit that a real group tree never reaches.
    """

    reset_project(ctx)
    on_disk = ctx.fixtures.grouped.stat().st_size

    started = time.monotonic()
    record = ctx.run("project.open", {"path": str(ctx.fixtures.grouped), "discard_current": True})
    elapsed = time.monotonic() - started

    equal(
        record.status,
        ExecutionStatus.SUCCEEDED,
        "project.open on a project with a deep layer-tree group",
    )
    values = result_of(record)
    equal(values["layer_count"], 1, "the layers of the grouped project")
    require(elapsed < 20.0, f"the {on_disk} byte project took {elapsed:.2f}s")
    reset_project(ctx)
    return (
        f"a {on_disk} byte project with {fixtures.NESTED_GROUPS} nested layer-tree groups "
        f"(depth ~205 in the written XML, measured): SUCCEEDED with {values['layer_count']} "
        f"layer in {elapsed * 1000:.0f} ms; the scan's limit is {MAX_PROJECT_DEPTH} levels"
    )


def at_44_field_sum_refuses_a_non_finite_value(ctx: Context) -> str:
    """NaN 和 Infinity 不是 JSON，所以结果里会带上它们的求和只能拒。

    NaN and Infinity are not JSON, so a sum that would carry them is refused.
    """

    from qgis.core import QgsFeature, QgsProject, QgsVectorLayer

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


def at_45_open_refuses_a_source_that_a_child_element_carries_in_its_tail(ctx: Context) -> str:
    """元素的 text 是整个子树（含子元素的 tail），不是只算直接文本。

    An element's text is the whole subtree, tail included.
    """

    from qgis.core import QgsProject

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at45 kept")
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    target = ctx.fixtures.root / "tail source.qgs"
    fixtures.write_mixed_content_remote(target, ctx.fixtures.raster, port)

    connections = 0
    started = time.monotonic()
    try:
        record = ctx.run("project.open", {"path": str(target), "discard_current": True})
        elapsed = time.monotonic() - started
        try:
            connection, _ = listener.accept()
        except TimeoutError:
            pass
        else:
            connections += 1
            connection.close()
    finally:
        listener.close()

    equal(record.status, ExecutionStatus.FAILED, "project.open on a tail-carried source")
    equal(error_code(record), PATH_NOT_ALLOWED, "the code for a tail-carried source")
    error = record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    require("remote data source" in error.message, f"the message: {error.message!r}")
    equal(connections, 0, "TCP connections made while the project was opened")
    require(elapsed < 1.0, f"the {target.stat().st_size} byte project took {elapsed:.2f}s")
    equal(project_layer_ids(), before, "the project's layers after the refusal")
    equal(QgsProject.instance().fileName(), file_before, "the project's file after the refusal")
    reset_project(ctx)
    return (
        "<datasource><x/>/vsicurl/http://127.0.0.1:<a port this case listens on>: "
        f"FAILED(PATH_NOT_ALLOWED) in {elapsed * 1000:.0f} ms with {connections} TCP "
        "connections. QDom datasource.text() is the whole subtree, so read() saw the source; "
        "the scan only read element.text and made 42 HTTP requests (measured). The old "
        "project kept its layer"
    )


def at_46_open_refuses_a_project_that_declares_xml_entities(ctx: Context) -> str:
    """内部 DTD 子集在任何预算之前就被展开，所以只能从扫描里拒。

    An internal DTD subset is expanded before any budget, so it is refused.
    """

    from qgis.core import QgsProject

    reset_project(ctx)
    on_disk = ctx.fixtures.entity_project.stat().st_size

    # 先量出文件对任何解析器的代价：read() 会展开它并接受它，所以拒绝必须来自扫描，发生在把文件
    # 交给 read() 之前。
    # This measures what the file costs whoever parses it: read() expands it and accepts it, so the
    # refusal has to come from the scan, before read() is handed the file.
    reader = QgsProject()
    started = time.monotonic()
    read_result = reader.read(str(ctx.fixtures.entity_project))
    read_elapsed = time.monotonic() - started
    reader.clear()
    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at46 kept")
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()

    started = time.monotonic()
    record = ctx.run(
        "project.open", {"path": str(ctx.fixtures.entity_project), "discard_current": True}
    )
    elapsed = time.monotonic() - started

    equal(record.status, ExecutionStatus.FAILED, "project.open on a project declaring entities")
    equal(error_code(record), PATH_NOT_ALLOWED, "the code for a project declaring entities")
    error = record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    require("entities" in error.message, f"the message: {error.message!r}")
    require(elapsed < 0.5, f"the {on_disk} byte project took {elapsed:.2f}s")
    equal(project_layer_ids(), before, "the project's layers after the refusal")
    equal(QgsProject.instance().fileName(), file_before, "the project's file after the refusal")
    reset_project(ctx)
    return (
        f"a {on_disk} byte project whose 1 MiB entity is read 50 times: read() "
        f"{'accepted' if read_result else 'refused'} it in {read_elapsed:.2f} s (expanded to "
        f"50 MiB), while project.open returned FAILED(PATH_NOT_ALLOWED) with 'declares XML "
        f"entities' in {elapsed * 1000:.0f} ms, before read(). The old scan parsed the "
        "expansion and peaked at 100 MiB (measured); this process kept answering and the "
        "old project kept its layer"
    )


def at_47_open_reads_projects_the_old_scan_called_remote(ctx: Context) -> str:
    """CSV 图层的 ``file:`` 源不算远程；图层元数据里的 URL 算。

    A CSV layer's ``file:`` source is not remote; a URL in the layer metadata is.
    """

    from qgis.core import QgsProject

    reset_project(ctx)
    csv_project = ctx.fixtures.root / "csv project.qgs"
    fixtures.write_csv_project(csv_project, ctx.fixtures.root)
    metadata_project = ctx.fixtures.root / "metadata project.qgs"
    fixtures.write_metadata_project(metadata_project, ctx.fixtures.cities, fixtures.CITIES_LAYER)

    started = time.monotonic()
    record = ctx.run("project.open", {"path": str(csv_project), "discard_current": True})
    csv_elapsed = time.monotonic() - started
    equal(record.status, ExecutionStatus.SUCCEEDED, "project.open on a CSV layer spelled file:./")
    csv_layers = result_of(record)["layer_count"]
    require(csv_layers >= 1, f"the CSV project's layers: {csv_layers}")
    require(csv_elapsed < 20.0, f"the CSV project took {csv_elapsed:.2f}s")

    # 这条规则不看元素名，所以标识符本身就是一个 URL 的图层也会在 read() 之前被拒：一个已知并接受
    # 下来的误拒——宁可误拒一个正常工程，也不让网络地址走到 read()。
    # The rule does not look at the element name, so a layer whose identifier is a URL is refused
    # before read() as well: a known false refusal that is accepted, because refusing a normal
    # project beats letting a network address reach read().
    ctx.add_layer(ctx.fixtures.cities, "at47 kept")
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()
    started = time.monotonic()
    metadata_record = ctx.run(
        "project.open", {"path": str(metadata_project), "discard_current": True}
    )
    metadata_elapsed = time.monotonic() - started

    equal(metadata_record.status, ExecutionStatus.FAILED, "project.open on a metadata identifier")
    equal(error_code(metadata_record), PATH_NOT_ALLOWED, "the code for a metadata identifier")
    error = metadata_record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    require("remote data source" in error.message, f"the message: {error.message!r}")
    require(metadata_elapsed < 1.0, f"the metadata project took {metadata_elapsed:.2f}s")
    equal(project_layer_ids(), before, "the project's layers after the refusal")
    equal(QgsProject.instance().fileName(), file_before, "the project's file after the refusal")
    reset_project(ctx)
    return (
        f"a Delimited Text layer spelled file:./points.csv: SUCCEEDED with {csv_layers} "
        f"layer in {csv_elapsed * 1000:.0f} ms; a layer whose metadata identifier is "
        "https://example.org/datasets/42: FAILED(PATH_NOT_ALLOWED) with 'remote data "
        f"source' in {metadata_elapsed * 1000:.0f} ms, before read() -- the accepted "
        "false refusal of the uniform rule (docs/OPERATIONS.md section 12). The CSV "
        "project was FAILED(PATH_NOT_ALLOWED) before A6 #3 while read() accepted it in "
        "0.06 s (measured): a file: prefix is not a scheme that reaches a host"
    )


def at_48_open_refuses_a_source_split_across_child_elements(ctx: Context) -> str:
    """datasource 的文本是它整个子树的字符数据，不是只算直接文本。

    The text of a datasource is every character data event of its subtree.
    """

    from qgis.core import QgsProject

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at48 kept")
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]

    connections = 0
    evidence: list[str] = []
    try:
        for suffix in (".qgs", ".qgz"):
            target = ctx.fixtures.root / f"split source{suffix}"
            fixtures.write_split_content_remote(target, ctx.fixtures.raster, port)
            started = time.monotonic()
            record = ctx.run("project.open", {"path": str(target), "discard_current": True})
            elapsed = time.monotonic() - started
            equal(record.status, ExecutionStatus.FAILED, f"project.open on a split source {suffix}")
            equal(error_code(record), PATH_NOT_ALLOWED, f"the code for a split source {suffix}")
            error = record.error
            if error is None:
                raise AssertionError("a failed record carries an error")
            require("remote data source" in error.message, f"the message: {error.message!r}")
            require(elapsed < 1.0, f"the {target.stat().st_size} byte project took {elapsed:.2f}s")
            evidence.append(f"{suffix} in {elapsed * 1000:.0f} ms")
        while True:
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                break
            connections += 1
            connection.close()
    finally:
        listener.close()

    equal(connections, 0, "TCP connections made while the projects were opened")
    equal(project_layer_ids(), before, "the project's layers after the refusal")
    equal(QgsProject.instance().fileName(), file_before, "the project's file after the refusal")
    reset_project(ctx)
    return (
        "<datasource>/v<x/>sicurl/htt<y/>p:<y2/>/<z/>/127.0.0.1:<a port this case listens on>"
        f"/never.tif: FAILED(PATH_NOT_ALLOWED) for {' and '.join(evidence)} with {connections} "
        "TCP connections. Every fragment is an ordinary piece of a path; their concatenation "
        "is the /vsicurl address QDom datasource.text() hands the provider, and the scan asked "
        "each fragment separately while read() made 8 connections before the refusal (measured)"
    )


def at_49_open_reads_a_project_whose_source_is_inside_a_vsi_directory(ctx: Context) -> str:
    """``/vsi`` 只有在数据源开头才是虚拟文件系统；目录名叫 vsi_data 就是本地文件。

    ``/vsi`` names a virtual file system only at the start of a source.
    """

    reset_project(ctx)
    target = ctx.fixtures.root / "vsi directory project.qgs"
    fixtures.write_vsi_directory_project(target, ctx.fixtures.cities, fixtures.CITIES_LAYER)

    started = time.monotonic()
    record = ctx.run("project.open", {"path": str(target), "discard_current": True})
    elapsed = time.monotonic() - started

    equal(record.status, ExecutionStatus.SUCCEEDED, "project.open on a source inside vsi_data")
    values = result_of(record)
    require(values["layer_count"] == 1, f"the layers of the project: {values['layer_count']}")
    require(elapsed < 20.0, f"the project took {elapsed:.2f}s")
    layers = project_layer_ids()
    reset_project(ctx)
    return (
        "a project whose datasource sits in a directory named vsi_data: SUCCEEDED with "
        f"{values['layer_count']} layer in {elapsed * 1000:.0f} ms (it registered {len(layers)}). "
        "/vsi is a GDAL file system only when it opens the path: the substring check refused the "
        "project as a remote data source, while read() reads it (measured)"
    )


def at_50_open_refuses_a_subdataset_that_names_a_remote_file(ctx: Context) -> str:
    """数据源自己的语法里写着远程文件时，在 read() 之前就拒。

    A remote file inside a datasource's own syntax is refused before read().
    """

    from qgis.core import QgsProject

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at50 kept")
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]

    connections = 0
    evidence: list[str] = []
    try:
        for suffix in (".qgs", ".qgz"):
            target = ctx.fixtures.root / f"subdataset remote{suffix}"
            fixtures.write_subdataset_remote_project(target, ctx.fixtures.raster, port)
            started = time.monotonic()
            record = ctx.run("project.open", {"path": str(target), "discard_current": True})
            elapsed = time.monotonic() - started
            equal(
                record.status,
                ExecutionStatus.FAILED,
                f"project.open on a remote subdataset {suffix}",
            )
            equal(
                error_code(record),
                PATH_NOT_ALLOWED,
                f"the code for a remote subdataset {suffix}",
            )
            error = record.error
            if error is None:
                raise AssertionError("a failed record carries an error")
            require("remote data source" in error.message, f"the message: {error.message!r}")
            require(elapsed < 1.0, f"the {target.stat().st_size} byte project took {elapsed:.2f}s")
            evidence.append(f"{suffix} in {elapsed * 1000:.0f} ms")
        while True:
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                break
            connections += 1
            connection.close()
    finally:
        listener.close()

    equal(connections, 0, "TCP connections made while the projects were opened")
    equal(project_layer_ids(), before, "the project's layers after the refusal")
    equal(QgsProject.instance().fileName(), file_before, "the project's file after the refusal")
    reset_project(ctx)
    return (
        '<datasource>NETCDF:"/vsicurl/http://127.0.0.1:<a port this case listens on>'
        '/never.tif":temperature</datasource>: FAILED(PATH_NOT_ALLOWED) for '
        f"{' and '.join(evidence)} with {connections} TCP connections. The marker is not at the "
        "start of the value, so the scan that asked only the start let read() fetch it (5 "
        "connections, measured, A12 #1)"
    )


def at_51_open_reads_a_project_whose_layer_is_a_local_subdataset(ctx: Context) -> str:
    """本地 GDAL 子数据集按它自己点名的文件读，与拼法无关。

    A local GDAL subdataset is read through the file it names, whatever its spelling.
    """

    reset_project(ctx)
    target = ctx.fixtures.root / "local subdataset.qgs"
    fixtures.write_subdataset_project(target, ctx.fixtures.raster)

    started = time.monotonic()
    record = ctx.run("project.open", {"path": str(target), "discard_current": True})
    elapsed = time.monotonic() - started
    equal(record.status, ExecutionStatus.SUCCEEDED, "project.open on a local subdataset")
    values = result_of(record)
    require(values["layer_count"] == 1, f"the layers of the project: {values['layer_count']}")
    equal(values["invalid_layer_count"], 0, "the invalid layers of the project")
    kept = project_layer_ids()
    require(len(kept) == 1, f"the registered layers: {kept}")
    reset_project(ctx)
    return (
        "a project whose layer is GTIFF_DIR:1:<the fixture>/cities.tif: SUCCEEDED with "
        f"{values['layer_count']} layer in {elapsed * 1000:.0f} ms and the layer kept; the "
        "subdataset spelling reaches the provider as it was written"
    )


# -------------------- Processing、QML 类别与渲染器 / Processing, QML categories and renderers

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
    "Labeling",
    "MapTips",
    "AttributeTable",
    "Rendering",
    "CustomProperties",
    "Temporal",
    "Elevation",
    "Notes",
]

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

# 验收自己注册一个 Processing 提供者：这是唯一能走到第三方提供者路径的办法，因为这台机器上
# QGIS 自带的算法都没有这个形状（实测）。经 ``addProvider`` 加进来的提供者在插件看来就是第三方，
# 它的算法走普通路径——先被发现，再用 ``run()`` 执行。
# The acceptance registers its own Processing provider: it is the only way to reach a third-party
# path, because no algorithm QGIS ships here has that shape (measured).  A provider added through
# ``addProvider`` is third-party as far as the plugin can see, and its algorithms take the
# ordinary path: discovery, then ``run()`` like every other algorithm.
PROBE_PROVIDER = "p5probe"
PROBE_ALGORITHMS = (
    f"{PROBE_PROVIDER}:non_json",
    f"{PROBE_PROVIDER}:runs",
)
# 记录探针算法被要求做了什么，这样用例才能证明图层真的被交接了，而且是在结果被拒之前。
# What the probe algorithms were asked to do, so a case can show that the transfer really happened
# before the result was refused.
PROBE_CALLS: list[str] = []
# 原生注册表接管提供者的所有权，所以这里必须让 Python 包装对象活着，与
# ``_REGISTERED_RENDERER_METADATA`` 是同一个理由。
# The native registry takes ownership of a provider, so the Python wrapper has to stay alive here
# for the same reason the renderer registry one does.
_registered_provider: list[Any] = []


def processing_probe_provider() -> None:
    """登记验收自己的提供者一次；拿不到它就拒跑。

    这些类写在这个函数里，是因为本模块不得在模块级 import QGIS（仓库的导入规则）；只有注册表自己
    拒了提供者（``addProvider()`` 返回假）才抛 ``Skip``，那是唯一会让第三方路径无法度量的情形。

    Register the acceptance's own provider once, and refuse to run without it.  The classes
    live inside this function because this module may not import QGIS at module level (the
    repository's import rule), and ``Skip`` is raised only when the registry itself refuses the
    provider (``addProvider()`` returning false), which is the one condition that would leave
    the third-party paths unmeasurable.
    """

    if _registered_provider:
        return
    from qgis.core import (
        QgsApplication,
        QgsProcessingAlgorithm,
        QgsProcessingOutputNumber,
        QgsProcessingOutputVectorLayer,
        QgsProcessingProvider,
        QgsVectorLayer,
    )

    class NonJsonResult(QgsProcessingAlgorithm):
        """先交出一个真的结果图层，再交一个没有 JSON 映像的值。

        Hands over a real result layer, then a value with no JSON image.
        """

        def name(self) -> str:
            return "non_json"

        def displayName(self) -> str:
            return "P5 probe: non-JSON result"

        def createInstance(self):  # noqa: N802 - the native spelling
            return NonJsonResult()

        def initAlgorithm(self, config=None):  # noqa: N802 - the native spelling
            self.addOutput(QgsProcessingOutputVectorLayer("LAYER", "the layer it hands over"))

        def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
            PROBE_CALLS.append(f"{PROBE_PROVIDER}:non_json.processAlgorithm")
            layer = QgsVectorLayer("Point?field=p5:string", "p5 probe result", "memory")
            context.temporaryLayerStore().addMapLayer(layer)
            # ``LAYER`` 放在前面，让插件真的把它交接过去；随后是一个没有无损 JSON 映像的值，拒绝
            # 必须把那次交接再撤回。
            # ``LAYER`` comes first, so the plugin really transfers it; then a value with no
            # lossless JSON image, so the refusal must undo that transfer again.
            return {"LAYER": layer.id(), "OBJECT": object()}

    class Runs(QgsProcessingAlgorithm):
        """一个真的跑起来并返回 JSON 的第三方算法。

        A third-party algorithm that really runs and returns JSON.
        """

        def name(self) -> str:
            return "runs"

        def displayName(self) -> str:
            return "P5 probe: runs"

        def createInstance(self):  # noqa: N802 - the native spelling
            return Runs()

        def initAlgorithm(self, config=None):  # noqa: N802 - the native spelling
            self.addOutput(QgsProcessingOutputNumber("VALUE", "a number"))

        def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
            PROBE_CALLS.append(f"{PROBE_PROVIDER}:runs.processAlgorithm")
            return {"VALUE": 42}

    class AcceptanceProvider(QgsProcessingProvider):
        def id(self) -> str:
            return PROBE_PROVIDER

        def name(self) -> str:
            return "P5 acceptance probes"

        def loadAlgorithms(self) -> None:  # noqa: N802 - the native spelling
            for algorithm in (NonJsonResult(), Runs()):
                self.addAlgorithm(algorithm)

    provider = AcceptanceProvider()
    if not QgsApplication.processingRegistry().addProvider(provider):
        raise Skip(
            "the processing registry refused the acceptance's own provider (addProvider() "
            "returned false), so the third-party paths of P01/P02/P15 cannot be measured here"
        )
    _registered_provider.append(provider)


def p01_list_algorithms(ctx: Context) -> str:
    require_processing()
    processing_probe_provider()
    from qgis.core import Qgis, QgsSettings

    algorithms = processing_algorithms()
    # 工具箱总会挡掉 HideFromToolbox；KnownIssues 只在对应开关关着时挡掉（默认关着）。
    # The toolbox always hides HideFromToolbox; it hides KnownIssues only while the matching
    # switch is off (off by default).
    excluded = int(Qgis.ProcessingAlgorithmFlag.HideFromToolbox)
    shown = (
        str(
            QgsSettings().value("Processing/Configuration/SHOW_ALGORITHMS_KNOWN_ISSUES", False)
        ).lower()
        == "true"
    )
    if not shown:
        excluded |= int(Qgis.ProcessingAlgorithmFlag.KnownIssues)
    visible = {
        algorithm.id(): algorithm
        for algorithm in algorithms
        if not int(algorithm.flags()) & excluded
    }
    hidden = [algorithm.id() for algorithm in algorithms if algorithm.id() not in visible]
    require(
        bool(hidden),
        "no registered algorithm carries HideFromToolbox or a hidden KnownIssues flag, so the "
        "filter is not measurable here",
    )

    record = ctx.run("processing.list_algorithms", {})
    equal(record.status, ExecutionStatus.SUCCEEDED, "processing.list_algorithms status")
    summaries = result_of(record)["algorithms"]
    ids = [summary["id"] for summary in summaries]

    expected = sorted(
        (algorithm.group(), algorithm.displayName(), algorithm.id())
        for algorithm in visible.values()
    )
    # 列表是扁平投影，按（分组名、算法名、id）排序；不是工具箱的 provider/分组树序。顺序不是
    # 判据，这条用例只钉住列表自己声明的那个键。
    # The list is a flat projection ordered by (group, display name, id), not the toolbox's
    # provider/group tree; order is not a criterion, and this case pins only the key the list
    # declares.
    equal(
        [(summary["group"], summary["name"], summary["id"]) for summary in summaries],
        expected,
        "the flat (group, display name, id) order",
    )
    equal(ids, [algorithm_id for _, _, algorithm_id in expected], "the visible ids")
    equal(len(ids), len(set(ids)), "duplicate ids")
    for summary in summaries:
        equal(sorted(summary), ["group", "id", "name"], "the fields of one summary")
    sample = next(summary for summary in summaries if summary["id"] == "native:buffer")
    equal(
        sample["name"],
        visible["native:buffer"].displayName(),
        "a display name from the registry",
    )
    equal(sample["group"], visible["native:buffer"].group(), "a group from the registry")
    for algorithm_id in PROBE_ALGORITHMS:
        require(
            algorithm_id in ids,
            f"the third-party algorithm {algorithm_id} is missing from the list",
        )
    prefixes = {algorithm_id.split(":")[0] for algorithm_id in ids}
    require(len(prefixes) > 1, f"only one provider prefix in the list: {sorted(prefixes)}")
    return (
        f"{len(ids)} algorithms in the flat (group, display name, id) order -- not the toolbox "
        f"tree -- exactly the live registry minus the {len(hidden)} carrying HideFromToolbox or "
        f"KnownIssues ({', '.join(sorted(hidden)[:3])}…), over {len(prefixes)} provider prefixes "
        f"({', '.join(sorted(prefixes))}), three fields each"
    )


def p02_no_algorithm_allow_list(ctx: Context) -> str:
    require_processing()
    processing_probe_provider()
    algorithms = processing_algorithms()

    refused = []
    for algorithm in algorithms:
        record = ctx.run("processing.get_algorithm_info", {"algorithm_id": algorithm.id()})
        if record.status is not ExecutionStatus.SUCCEEDED:
            code = record.error.code if record.error is not None else str(record.status)
            refused.append(f"{algorithm.id()}: {code}")
    require(not refused, f"the definition of {len(refused)} algorithms was refused: {refused[:3]}")

    # 本次运行自己注册的提供者就是这里的第三方例子。
    # A provider this run registered is the third-party example here.
    third_party = run_algorithm(ctx, f"{PROBE_PROVIDER}:runs", {})
    equal(
        third_party.status,
        ExecutionStatus.SUCCEEDED,
        f"{PROBE_PROVIDER}:runs failed: {third_party.error}",
    )
    equal(outputs_of(third_party), {"VALUE": 42}, "the third-party run's outputs")

    # 还要测一个 QGIS 自带、但不属于 'native' 的提供者，如果这台机器上装了一个。
    # A provider QGIS ships itself but that is not 'native', when this machine has one.
    extra = "no QGIS-shipped non-native provider with a runnable algorithm is installed here"
    shipped = next((item for item in algorithms if item.id() == "3d:tessellate"), None)
    if shipped is not None:
        layer_id = cities_layer(ctx, "p02 cities")
        record = run_algorithm(ctx, shipped.id(), {"INPUT": layer_id, "OUTPUT": "memory:"})
        equal(record.status, ExecutionStatus.SUCCEEDED, f"{shipped.id()} failed: {record.error}")
        equal(shipped.provider().id(), "3d", "the provider of the algorithm that ran")
        layer = output_layer(ctx, record, "OUTPUT")
        require(layer.isValid(), "the result of a non-native provider is not a valid layer")
        extra = (
            f"provider '3d' also ran {shipped.id()} from a project layer into memory "
            f"({layer.featureCount()} features in the project)"
        )
    return (
        f"all {len(algorithms)} registered algorithms answered get_algorithm_info, so discovery "
        f"filters nothing; the provider this run registered ran {PROBE_PROVIDER}:runs for real "
        f"(VALUE=42); {extra}"
    )


def p03_algorithm_info_fields(ctx: Context) -> str:
    require_processing()
    from qgis.core import (
        QgsApplication,
        QgsProcessingParameterDefinition,
        QgsProcessingParameterEnum,
    )

    registry = QgsApplication.processingRegistry()
    six_fields = ["accepted_types", "description", "help", "name", "options", "type"]
    parameters_seen = 0
    outputs_seen = 0
    for algorithm_id in (
        "native:buffer",
        "native:zonalstatisticsfb",
        "native:basicstatisticsforfields",
    ):
        algorithm = registry.algorithmById(algorithm_id)
        require(algorithm is not None, f"{algorithm_id} is not registered here")
        visible = [
            definition
            for definition in algorithm.parameterDefinitions()
            if not definition.flags() & QgsProcessingParameterDefinition.FlagHidden
        ]
        info = algorithm_info(ctx, algorithm_id)
        equal(
            sorted(info),
            ["algorithm", "outputs", "parameters"],
            f"{algorithm_id}: the result fields",
        )
        details = info["algorithm"]
        equal(
            sorted(details),
            ["help", "id", "name", "short_description"],
            f"{algorithm_id}: the algorithm fields",
        )
        equal(details["id"], algorithm.id(), f"{algorithm_id}: id")
        equal(details["name"], algorithm.displayName(), f"{algorithm_id}: name")
        equal(
            details["short_description"],
            algorithm.shortDescription() or "",
            f"{algorithm_id}: short_description",
        )
        equal(details["help"], algorithm.shortHelpString() or "", f"{algorithm_id}: help")
        equal(
            [parameter["name"] for parameter in info["parameters"]],
            [definition.name() for definition in visible],
            f"{algorithm_id}: the native parameter order with the hidden ones skipped",
        )
        for parameter, definition in zip(info["parameters"], visible, strict=True):
            name = f"{algorithm_id}.{definition.name()}"
            equal(sorted(parameter), six_fields, f"{name}: the six fields")
            equal(parameter["type"], definition.__class__.__name__, f"{name}: type")
            equal(parameter["description"], definition.description() or "", f"{name}: description")
            equal(parameter["help"], definition.help() or "", f"{name}: help")
            if isinstance(definition, QgsProcessingParameterEnum):
                equal(parameter["options"], list(definition.options()), f"{name}: option order")
            else:
                equal(parameter["options"], None, f"{name}: options on a non-enum parameter")
            parameter_type = registry.parameterType(definition.type())
            equal(
                parameter["accepted_types"],
                [] if parameter_type is None else list(parameter_type.acceptedPythonTypes()),
                f"{name}: accepted_types",
            )
            parameters_seen += 1
        equal(
            [(output["name"], output["type"], output["description"]) for output in info["outputs"]],
            [
                (output.name(), output.__class__.__name__, output.description() or "")
                for output in algorithm.outputDefinitions()
            ],
            f"{algorithm_id}: the native output order, the class name as the type",
        )
        outputs_seen += len(info["outputs"])

    # 隐藏参数确实被略过：这次运行会找一个真的藏了参数的算法来验证。
    # A hidden parameter really is left out: this run finds an algorithm that hides one.
    hidden_algorithm = next(
        (
            algorithm
            for algorithm in processing_algorithms()
            if any(
                definition.flags() & QgsProcessingParameterDefinition.FlagHidden
                for definition in algorithm.parameterDefinitions()
            )
        ),
        None,
    )
    if hidden_algorithm is None:
        raise Skip(
            "no registered algorithm hides a parameter here, so nothing would prove the skip"
        )
    hidden = [
        definition.name()
        for definition in hidden_algorithm.parameterDefinitions()
        if definition.flags() & QgsProcessingParameterDefinition.FlagHidden
    ]
    listed = [
        parameter["name"] for parameter in algorithm_info(ctx, hidden_algorithm.id())["parameters"]
    ]
    require(
        not set(hidden) & set(listed),
        f"{hidden_algorithm.id()}: a hidden parameter was reported anyway: {listed}",
    )

    zonal = parameters_by_name(algorithm_info(ctx, "native:zonalstatisticsfb"))
    enum_options = zonal["STATISTICS"]["options"]
    require(
        isinstance(enum_options, list) and len(enum_options) > 1,
        f"an enum's options did not survive: {enum_options!r}",
    )
    equal(zonal["COLUMN_PREFIX"]["options"], None, "options are null for a string parameter")
    return (
        f"{parameters_seen} parameters and {outputs_seen} outputs of three algorithms read back "
        f"field by field against the live definitions (six fields, native order, the class name "
        f"as the type, acceptedPythonTypes as accepted_types, an enum's option order, null "
        f"options elsewhere, four algorithm fields); {hidden_algorithm.id()} hides "
        f"{len(hidden)} parameter(s) ({', '.join(hidden)}) and none of them is reported; "
        f"STATISTICS keeps its {len(enum_options)} options and COLUMN_PREFIX answers null"
    )


def p04_unknown_algorithm(ctx: Context) -> str:
    require_processing()
    missing = "native:p5-acceptance-not-an-algorithm"
    help_sentence = f'Algorithm "{missing}" not found.'
    run_sentence = f"Error: Algorithm {missing} not found\n"
    before = project_layer_ids()

    info = ctx.run("processing.get_algorithm_info", {"algorithm_id": missing})
    equal(info.status, ExecutionStatus.FAILED, "get_algorithm_info on an unknown id")
    equal(error_code(info), INVALID_INPUT, "the code for an unknown algorithm")
    equal(message_of(info), help_sentence, "the get_algorithm_info sentence")

    # 运行路径自己解析 id：未知 id 在调用 ``processing.run()`` 之前就被拒，仍是调用方的输入错误。
    # The run path resolves the id itself: an unknown id is refused before ``processing.run()``
    # and stays a caller input mistake.
    run = run_algorithm(ctx, missing, {})
    equal(run.status, ExecutionStatus.FAILED, "run_algorithm on an unknown id")
    equal(error_code(run), INVALID_INPUT, "the run code for an unknown algorithm")
    equal(message_of(run), run_sentence, "the run sentence")
    equal(project_layer_ids(), before, "the layers after the unknown id")
    for record in (info, run):
        require(record.result is None, "a refused call carries a result")
    return (
        f"an unknown id is FAILED(INVALID_INPUT) from both operations, each with its own GUI "
        f"action's sentence: {help_sentence!r} from get_algorithm_info (algorithmHelp's "
        f"printout) and {run_sentence!r} from run_algorithm (the run path's exception, newline "
        "included); neither call carries a result or adds a layer"
    )


def p05_unknown_key(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p05 cities")
    before = project_layer_ids()
    parameters = {
        "INPUT": layer_id,
        "DISTANCE": 1.0,
        "SEGMENTS": 8,
        "DISSOLVE": False,
        "OUTPUT": "memory:",
        "P5_NOT_A_PARAMETER": 1,
    }

    record = run_algorithm(ctx, "native:buffer", parameters)
    equal(
        record.status,
        ExecutionStatus.SUCCEEDED,
        f"a run with an undeclared parameter: {record.error}",
    )
    layer = output_layer(ctx, record, "OUTPUT")
    require(layer.isValid(), "the buffer result is not a valid layer")
    equal(layer.featureCount(), 5, "one buffer per input point")
    equal(len(project_layer_ids()), len(before) + 1, "the layers after the run")
    return (
        "P5_NOT_A_PARAMETER reached QGIS untouched and native:buffer SUCCEEDED, adding a valid "
        f"{layer.featureCount()}-feature layer to the project: the plugin has no parameter-name "
        "whitelist any more, so an undeclared key is QGIS's to ignore"
    )


def p06_buffer_memory_output(ctx: Context) -> str:
    require_processing()
    from qgis.core import Qgis

    layer_id = cities_layer(ctx, "p06 cities")
    before = project_layer_ids()
    record = run_algorithm(
        ctx,
        "native:buffer",
        {
            "INPUT": layer_id,
            "DISTANCE": 1.0,
            "SEGMENTS": 8,
            "DISSOLVE": False,
            "OUTPUT": "memory:",
        },
    )
    equal(record.status, ExecutionStatus.SUCCEEDED, f"native:buffer failed: {record.error}")
    equal(sorted(outputs_of(record)), ["OUTPUT"], "the native output keys")
    layer = output_layer(ctx, record, "OUTPUT")
    require(layer.isValid(), "the buffer result is not a valid layer")
    equal(layer.geometryType(), Qgis.GeometryType.Polygon, "the geometry of the result")
    equal(layer.featureCount(), 5, "one buffer per input point")
    equal(sorted(project_layer_ids()), sorted([*before, layer.id()]), "the project's layers")
    pump(ctx.app, 0.05)
    require(
        project_layers().get(layer.id()) is layer, "the result layer went away with its context"
    )
    return (
        'OUTPUT="memory:" SUCCEEDED, its result layer was taken out of the temporary store and '
        f"registered in the project as {layer.id()[:8]}…, a valid polygon layer with "
        f"{layer.featureCount()} features that is still there after the event loop turned"
    )


def p07_array_input(ctx: Context) -> str:
    require_processing()
    first = cities_layer(ctx, "p07 first")
    second = cities_layer(ctx, "p07 second")
    require(first != second, "the two layer.add calls returned the same id")

    record = run_algorithm(
        ctx, "native:mergevectorlayers", {"LAYERS": [first, second], "OUTPUT": "memory:"}
    )
    equal(record.status, ExecutionStatus.SUCCEEDED, f"mergevectorlayers failed: {record.error}")
    layer = output_layer(ctx, record, "OUTPUT")
    equal(layer.featureCount(), 10, "the features of two five-feature layers merged from a list")
    return (
        "native:mergevectorlayers accepted a two-element JSON array of project layer ids "
        f"(LAYERS) and returned {layer.featureCount()} features, so a list parameter is passed "
        "through as the native structure"
    )


def p08_scalar_output(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p08 cities")

    record = run_algorithm(
        ctx, "native:basicstatisticsforfields", {"INPUT_LAYER": layer_id, "FIELD_NAME": "pop"}
    )
    equal(record.status, ExecutionStatus.SUCCEEDED, f"basicstatistics failed: {record.error}")
    outputs = outputs_of(record)
    equal(outputs["COUNT"], 3, "the non-null values of pop")
    equal(outputs["SUM"], fixtures.POP_TOTAL, "the sum of pop")
    require(
        abs(outputs["MEAN"] - fixtures.POP_TOTAL / 3) < 1e-9,
        f"the mean of pop: {outputs['MEAN']!r}",
    )
    for key in ("COUNT", "SUM", "MEAN", "MIN", "MAX"):
        require(
            type(outputs[key]) in (int, float),
            f"{key} is not a JSON number: {outputs[key]!r}",
        )

    # 桌面版两个输出控件的默认值都是“临时输出”，所以两个键都出现：一个临时图层、一个临时 HTML。
    # Both desktop output widgets default to a temporary output, so both keys appear: a temporary
    # layer and a temporary HTML file.
    statistics = output_layer(ctx, record, "OUTPUT")
    require(statistics.isValid(), "the statistics layer is not valid")
    equal(statistics.featureCount(), 1, "the statistics table's row count")
    html = outputs["OUTPUT_HTML_FILE"]
    require(isinstance(html, str), f"the HTML output is {html!r}, not a path")
    require(Path(html).is_file(), f"the HTML output {html!r} was not written")

    # ``None`` 是桌面版“跳过输出”的位置：两个键都不出现，标量照旧。
    # ``None`` is the desktop's "skip output": neither key appears, and the scalars stay.
    skipped = run_algorithm(
        ctx,
        "native:basicstatisticsforfields",
        {
            "INPUT_LAYER": layer_id,
            "FIELD_NAME": "pop",
            "OUTPUT": None,
            "OUTPUT_HTML_FILE": None,
        },
    )
    equal(skipped.status, ExecutionStatus.SUCCEEDED, f"skipped outputs: {skipped.error}")
    skipped_outputs = outputs_of(skipped)
    require(
        "OUTPUT" not in skipped_outputs and "OUTPUT_HTML_FILE" not in skipped_outputs,
        f"a skipped output still appeared: {sorted(skipped_outputs)}",
    )
    equal(skipped_outputs["SUM"], fixtures.POP_TOTAL, "the sum with both outputs skipped")
    return (
        f"a scalar-only run returned {len(outputs) - 2} finite JSON numbers (COUNT=3, "
        f"SUM=200.0, MEAN={outputs['MEAN']:.3f}) plus the desktop's two temporary outputs: the "
        f"statistics layer ({statistics.featureCount()} row) and the HTML file it wrote, while "
        'passing ``None`` for both destinations is the desktop\'s "skip output" and leaves the '
        "scalars only"
    )


def p09_multiple_outputs(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p09 cities")

    record = run_algorithm(
        ctx,
        "native:adduniquevalueindexfield",
        {
            "INPUT": layer_id,
            "FIELD": "name",
            "FIELD_NAME": "idx",
            "OUTPUT": "memory:",
            "SUMMARY_OUTPUT": "memory:",
        },
    )
    equal(record.status, ExecutionStatus.SUCCEEDED, f"adduniquevalueindexfield: {record.error}")
    equal(sorted(outputs_of(record)), ["OUTPUT", "SUMMARY_OUTPUT"], "the native output keys")
    first = output_layer(ctx, record, "OUTPUT")
    second = output_layer(ctx, record, "SUMMARY_OUTPUT")
    require(first.id() != second.id(), "both output keys name the same layer")
    require(first.isValid() and second.isValid(), "an output layer is not valid")
    require(
        "idx" in [field.name() for field in first.fields()],
        f"the new field is missing: {[field.name() for field in first.fields()]}",
    )
    equal(second.featureCount(), 5, "the summary has one feature per input feature")
    return (
        "both native output keys survived (OUTPUT and SUMMARY_OUTPUT) as two distinct, valid "
        f"project layers ({first.featureCount()} and {second.featureCount()} features), so no "
        "fixed OUTPUT-only mapping is assumed"
    )


def p10_json_values(ctx: Context) -> str:
    require_processing()
    from qgis.core import Qgis, QgsProject

    from qgis_agent_plugin.operations.processing import ProcessingRunAlgorithmParams

    accepted = ProcessingRunAlgorithmParams.model_validate(
        {
            "algorithm_id": "native:mergevectorlayers",
            "parameters": {"LAYERS": ["a", "b"], "DEEP": {"values": [1, {"flag": True}]}},
        }
    )
    equal(
        accepted.parameters["DEEP"],
        {"values": [1, {"flag": True}]},
        "a nested JSON object survived the input boundary",
    )

    refused = {
        "NaN": float("nan"),
        "infinity": float("inf"),
        "a Qt enum": Qgis.GeometryType.Point,
        "a QObject": QgsProject.instance(),
        "bytes": b"\x00",
        "an integer key": {1: "x"},
        "a nested NaN": ["a", [float("-inf")]],
    }
    for label, value in refused.items():
        try:
            ProcessingRunAlgorithmParams.model_validate(
                {"algorithm_id": "native:buffer", "parameters": {"DISTANCE": value}}
            )
        except ValidationError:
            continue
        raise AssertionError(f"the input boundary accepted {label}")

    layer_id = cities_layer(ctx, "p10 cities")
    before = project_layer_ids()
    record = run_algorithm(
        ctx,
        "native:buffer",
        {"INPUT": layer_id, "DISTANCE": float("nan"), "OUTPUT": "memory:"},
    )
    equal(record.status, ExecutionStatus.FAILED, "a run whose distance is NaN")
    equal(error_code(record), INVALID_INPUT, "the code for a NaN parameter")
    equal(project_layer_ids(), before, "the layers after the NaN refusal")
    return (
        "a nested JSON object and array are accepted and unchanged, while NaN, infinity, a Qt "
        "enum, a QObject, bytes, an integer dict key and a nested -inf are all refused with "
        "INVALID_INPUT before anything runs"
    )


def p12_input_path_policy(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p12 cities")
    outside = ctx.fixtures.outside / "secret.gpkg"
    require(outside.is_file(), "the outside fixture is missing")

    # 插件侧没有路径策略：任何本地绝对路径都原样到达 QGIS，接下来由 QGIS 决定。远程那一半故意
    # 不测——要测它就得发真请求，而没有用例会发。
    # There is no plugin-side path policy: any local absolute path reaches QGIS as it arrived, and
    # what happens next is QGIS's decision.  The remote half is not measured here on purpose:
    # measuring it would mean a real request, and no case makes one.
    outside_record = run_algorithm(
        ctx, "native:mergevectorlayers", {"LAYERS": [layer_id, str(outside)], "OUTPUT": "memory:"}
    )
    if outside_record.status is ExecutionStatus.SUCCEEDED:
        merged = output_layer(ctx, outside_record, "OUTPUT")
        outside_evidence = (
            f"native:mergevectorlayers read {outside.name} from outside the fixtures directory and "
            f"merged it into a layer with {merged.featureCount()} features (the project layer "
            f"plus the five features of the outside file)"
        )
    else:
        outside_evidence = (
            f"native:mergevectorlayers failed on {outside.name} with "
            f"{error_code(outside_record)} from QGIS's own layer loading"
        )
        require(
            error_code(outside_record) != PATH_NOT_ALLOWED,
            "the plugin still refuses a local file the caller named",
        )

    # 同一个边界的正面一半：CRS 权威码长得像 URI scheme，但它是 QGIS 解析的标识符，不是数据源。
    # The positive half of the same boundary: a CRS authority code has the shape of a URI scheme,
    # yet it is an identifier QGIS parses, not a data source.
    reprojected = run_algorithm(
        ctx,
        "native:reprojectlayer",
        {"INPUT": layer_id, "TARGET_CRS": "EPSG:32633", "OUTPUT": "memory:"},
    )
    equal(reprojected.status, ExecutionStatus.SUCCEEDED, f"reprojectlayer: {reprojected.error}")
    equal(
        output_layer(ctx, reprojected, "OUTPUT").crs().authid(),
        "EPSG:32633",
        "the CRS of the reprojected result",
    )
    return (
        f"{outside_evidence}, so no file is refused by the plugin any more; "
        "TARGET_CRS='EPSG:32633' on the same entry point SUCCEEDED and the result really is in "
        "EPSG:32633, so a CRS code is still handed through as data"
    )


def p13_destination_policy(ctx: Context) -> str:
    require_processing()
    from qgis.core import QgsProcessing, QgsVectorLayer

    layer_id = cities_layer(ctx, "p13 cities")
    target = ctx.fixtures.root / "p13 output.gpkg"

    # 写真实文件是调用方的决定：QGIS 就写到被告知的位置。``gdal:convertformat`` 是拷贝图层，所以
    # 写出的文件要带同样的要素与几何——只看行数会被几何全 NULL 的表骗过，而 ``gdal:buffervectors``
    # 在几何列名不匹配时留下的正是这种表。
    # A real file destination is the caller's decision: QGIS writes it where it was told.
    # ``gdal:convertformat`` copies the layer, so the written file has to carry the same features
    # and their geometry: a count alone is fooled by a table whose every geometry is NULL, which is
    # what ``gdal:buffervectors`` leaves behind when its geometry column name does not match.
    written = run_algorithm(
        ctx,
        "gdal:convertformat",
        {"INPUT": layer_id, "OUTPUT": str(target)},
    )
    equal(written.status, ExecutionStatus.SUCCEEDED, f"convertformat to a file: {written.error}")
    equal(outputs_of(written), {"OUTPUT": str(target)}, "the value of the output key")
    require(target.is_file(), f"{target.name} was not written")
    product = QgsVectorLayer(str(target), "p13 written", "ogr")
    require(product.isValid(), f"the written dataset {target.name} is not a readable layer")
    source = project_layer(layer_id)
    equal(product.featureCount(), source.featureCount(), "the features the written dataset holds")
    with_geometry = sum(
        1
        for feature in product.getFeatures()
        if feature.hasGeometry() and not feature.geometry().isNull()
    )
    equal(with_geometry, source.featureCount(), "the written features that really carry a geometry")
    equal(product.wkbType(), source.wkbType(), "the geometry type the written dataset keeps")

    # QGIS 自己的临时输出：插件把它产出的那个图层收进工程。
    # QGIS's own temporary output: the plugin takes the layer it produced.
    temporary = run_algorithm(
        ctx,
        "native:buffer",
        {"INPUT": layer_id, "DISTANCE": 1.0, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
    )
    equal(temporary.status, ExecutionStatus.SUCCEEDED, f"TEMPORARY_OUTPUT: {temporary.error}")
    require(output_layer(ctx, temporary, "OUTPUT").isValid(), "the temporary output layer")

    # 非文件目的地也由桌面版输出控件先判：GDAL 提供者不支持非文件输出，所以 "memory:" 在参数
    # 面板里就会被拒；插件用的是同一条判据、同一句话。
    # A non-file destination is judged by the desktop output widget first: the GDAL provider does
    # not support non-file outputs, so "memory:" is refused in the panel; the plugin uses the
    # same criterion and the same sentence.
    made_up = run_algorithm(
        ctx, "gdal:buffervectors", {"INPUT": layer_id, "DISTANCE": 1.0, "OUTPUT": "memory:"}
    )
    equal(made_up.status, ExecutionStatus.FAILED, "a memory destination on the gdal provider")
    equal(
        error_code(made_up),
        INVALID_INPUT,
        "the code for a memory destination on the gdal provider",
    )
    equal(
        message_of(made_up),
        "This algorithm only supports disk-based outputs",
        "the memory destination's sentence",
    )
    require(project_layers().get("memory:") is None, "the project holds a layer named 'memory:'")

    # 远程字形不在可写扩展名的判据里：它先过同一条输出控件判据，然后由 QGIS 去开；这个值指向
    # 本机关闭的端口，所以验收的请求出不了这台机器。
    # A remote spelling is not one the writable-extension criterion can judge: it passes the same
    # output-widget criterion and then QGIS opens it; this value names a closed local port, so the
    # acceptance request cannot leave the machine.
    remote = run_algorithm(
        ctx,
        "native:buffer",
        {"INPUT": layer_id, "DISTANCE": 1.0, "OUTPUT": fixtures.REMOTE_VSICURL},
    )
    equal(remote.status, ExecutionStatus.FAILED, "a remote destination")
    remote_code = error_code(remote)
    require(
        remote_code != PATH_NOT_ALLOWED,
        "the plugin still refuses a remote destination before QGIS sees it",
    )

    return (
        "a destination is judged by the desktop output widget and then handed to QGIS: "
        f"gdal:convertformat wrote a GeoPackage at the path the caller named "
        f"({target.stat().st_size} bytes) that opens with the source's {product.featureCount()} "
        "features, the source's wkb type and a non-null geometry on every one of them, and the "
        'outputs value is that same path; native:buffer with OUTPUT="TEMPORARY_OUTPUT" '
        "SUCCEEDED with its result layer in the project; gdal:buffervectors with "
        'OUTPUT="memory:" is FAILED(INVALID_INPUT) with the panel\'s own sentence (the GDAL '
        "provider supports disk-based outputs only) instead of QGIS's misleading SUCCEEDED with "
        f'the literal "memory:"; and a /vsicurl destination is FAILED({remote_code}) from QGIS, '
        "not PATH_NOT_ALLOWED from the plugin"
    )


def p14_output_ownership(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p14 cities")
    record = run_algorithm(
        ctx,
        "native:buffer",
        {"INPUT": layer_id, "DISTANCE": 0.5, "SEGMENTS": 8, "DISSOLVE": False, "OUTPUT": "memory:"},
    )
    equal(record.status, ExecutionStatus.SUCCEEDED, f"native:buffer failed: {record.error}")
    transfered = outputs_of(record)["OUTPUT"]
    layer = project_layer(transfered)
    equal(layer.id(), transfered, "the id the record reported is the project's own id")

    # 这次运行的 QgsProcessingContext 已经没了，图层必须活得比它长。
    # The run's QgsProcessingContext is gone by now; the layer has to outlive it.
    pump(ctx.app, 0.1)
    equal(project_layer(transfered).featureCount(), 5, "the features after the context was gone")
    info = ctx.run("layer.get_info", {"layer_id": transfered})
    equal(info.status, ExecutionStatus.SUCCEEDED, f"layer.get_info on the result: {info.error}")
    equal(result_of(info)["layer_id"], transfered, "the layer the project resolves the id to")
    return (
        f"the result layer {transfered[:8]}… belongs to the project, still resolves by the id the "
        "record reported after the processing context was destroyed, and layer.get_info reads it"
    )


def p15_non_json_output(ctx: Context) -> str:
    require_processing()
    processing_probe_provider()
    from qgis.core import QgsProject

    project = QgsProject.instance()
    added: list[str] = []
    removed: list[str] = []

    # 探针先交出的结果图层必须先进工程、再被搬掉：图层这一步只搬本次运行加进去的。
    # The result layer the probe hands over must enter the project and then be removed again: the
    # layer step undoes only what this run added.
    def note_added(layer: Any) -> None:
        added.append(layer.id())

    def note_removed(layer_id: str) -> None:
        removed.append(layer_id)

    before = project_layer_ids()
    project.layerWasAdded.connect(note_added)
    project.layerRemoved.connect(note_removed)
    try:
        record = run_algorithm(ctx, f"{PROBE_PROVIDER}:non_json", {})
    finally:
        project.layerWasAdded.disconnect(note_added)
        project.layerRemoved.disconnect(note_removed)

    equal(record.status, ExecutionStatus.FAILED, "a result with no lossless JSON image")
    equal(error_code(record), INTERNAL_ERROR, "the code for a result with no JSON image")
    require(record.result is None, "a failed run carries a result")
    require(
        f"{PROBE_PROVIDER}:non_json.processAlgorithm" in PROBE_CALLS,
        f"the plugin refused the run without calling the algorithm: {PROBE_CALLS}",
    )

    message = message_of(record)
    leaks = ("0x", "object at", "<object", "QgsVectorLayer", "\\")
    leaked = [leak for leak in leaks if leak in message]
    require(
        not leaked,
        f"the failure message describes the object or a path ({leaked}): {message!r}",
    )

    equal(len(added), 1, f"the result layer the probe handed over was added {len(added)} times")
    equal(removed, added, "the layers the failed run removed, against the ones it added")
    for layer_id in added:
        require(
            layer_id not in project_layers(),
            f"the layer {layer_id!r} of the failed run stayed in the project",
        )
    equal(project_layer_ids(), before, "the project's layers after the failed run")
    return (
        f"{PROBE_PROVIDER}:non_json handed over a real result layer and then an object; the run "
        f"is FAILED(INTERNAL_ERROR) with no result, the message names neither the object nor a "
        f"path ({message!r}), and the layer this run added ({added[0][:8]}…) was removed again, "
        f"so the project is back to its {len(before)} layers"
    )


def p16_submit_cancel_queued(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p16 cities")
    before = project_layer_ids()

    submitted = ctx.submit(
        "processing.run_algorithm",
        {
            "algorithm_id": "native:buffer",
            "parameters": {"INPUT": layer_id, "DISTANCE": 1.0, "OUTPUT": "memory:"},
        },
    )
    equal(submitted.status, ExecutionStatus.QUEUED, "the status right after submit")
    receipt = ctx.engine.cancel(submitted.execution_id)
    require(receipt.accepted, "the cancel of a queued run was refused")
    equal(receipt.status, ExecutionStatus.CANCELLED, "the status the cancel reported")

    pump(ctx.app, 0.2)
    record = ctx.record(submitted.execution_id)
    equal(record.status, ExecutionStatus.CANCELLED, "the status after the event loop turned")
    require(record.result is None, "a cancelled run carries a result")
    equal(project_layer_ids(), before, "the layers after the cancelled run")
    return (
        "a submitted processing run was QUEUED, cancelled before the event loop reached it, and "
        "stayed CANCELLED with no result and no layer; nothing was executed"
    )


def p17_gui_output_defaults(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p17 cities")
    before = project_layer_ids()
    parameters = {"INPUT": layer_id, "DISTANCE": 1.0, "SEGMENTS": 8, "DISSOLVE": False}

    # 参数面板总会给输出参数一个值，默认就是临时输出；插件补的是同一个值。
    # The parameter panel always carries a destination value, a temporary output by default; the
    # plugin fills in the same value.
    record = run_algorithm(ctx, "native:buffer", parameters)
    equal(record.status, ExecutionStatus.SUCCEEDED, f"a run without OUTPUT: {record.error}")
    outputs = outputs_of(record)
    equal(sorted(outputs), ["OUTPUT"], "the output keys")
    layer = output_layer(ctx, record, "OUTPUT")
    equal(layer.featureCount(), 5, "the buffered features")
    require(layer.isTemporary(), "the filled destination is not a temporary output")
    equal(sorted(project_layer_ids()), sorted([*before, layer.id()]), "the project's layers")
    return (
        "native:buffer without OUTPUT SUCCEEDED: the required destination got the desktop "
        f"panel's own default (a temporary output), so the {layer.featureCount()}-feature result "
        f"layer ({layer.id()[:8]}…) is in the project; the plugin reports the key it filled"
    )


def p18_parameter_value_refused(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p18 cities")
    before = project_layer_ids()

    # 面板逐参数问 ``checkValueIsAcceptable``，第一句不过的就带它的描述拒掉整次调用。
    # The panel asks ``checkValueIsAcceptable`` per parameter and refuses the whole call with the
    # first description that fails it.
    bad_type = run_algorithm(
        ctx, "native:buffer", {"INPUT": layer_id, "DISTANCE": "abc", "OUTPUT": "memory:"}
    )
    equal(bad_type.status, ExecutionStatus.FAILED, "a numeric parameter given text")
    equal(error_code(bad_type), INVALID_INPUT, "the code for a numeric parameter given text")
    equal(
        message_of(bad_type),
        "Wrong or missing parameter value: Distance",
        "the sentence for a bad number",
    )

    missing = run_algorithm(ctx, "native:buffer", {"DISTANCE": 1.0, "OUTPUT": "memory:"})
    equal(missing.status, ExecutionStatus.FAILED, "a required source left out")
    equal(error_code(missing), INVALID_INPUT, "the code for a required source left out")
    equal(
        message_of(missing),
        "Wrong or missing parameter value: Input layer",
        "the sentence for a missing source",
    )

    equal(project_layer_ids(), before, "the layers after both refusals")

    # PREDICATE 的控件默认值是 [0]；面板会把它一起放进 map，而 QGIS 自己对缺键的 enum 不回落
    # 到默认值，所以这条只有补了默认值才会成功。
    # PREDICATE's widget default is [0]; the panel carries it, and QGIS itself does not fall back
    # to the default for a missing enum key, so this succeeds only once the default is filled.
    first = cities_layer(ctx, "p18 first")
    second = cities_layer(ctx, "p18 second")
    defaulted = run_algorithm(
        ctx,
        "native:extractbylocation",
        {"INPUT": first, "INTERSECT": second, "OUTPUT": "memory:"},
    )
    equal(defaulted.status, ExecutionStatus.SUCCEEDED, f"the filled default: {defaulted.error}")
    require("OUTPUT" in outputs_of(defaulted), "the defaulted run produced no OUTPUT")
    return (
        "a text where native:buffer wants a number and a missing required source are both "
        "FAILED(INVALID_INPUT) with the panel's own sentences "
        f"({message_of(bad_type)!r} and {message_of(missing)!r}), and neither adds a layer; "
        "native:extractbylocation without PREDICATE SUCCEEDED because its widget default [0] was "
        "filled in (QGIS itself refuses the missing key)"
    )


def p19_output_extension_refused(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p19 cities")
    target = ctx.fixtures.root / "p19 output.zzz"

    # 输出控件会按提供者的可写格式判目的地；QGIS 自己写的时候会把 .zzz 补成 .gpkg，面板不会。
    # The output widget judges a destination by the provider's writable formats; QGIS itself would
    # append .gpkg to .zzz when writing, the panel does not.
    record = run_algorithm(
        ctx, "native:buffer", {"INPUT": layer_id, "DISTANCE": 1.0, "OUTPUT": str(target)}
    )
    equal(record.status, ExecutionStatus.FAILED, "an unsupported output extension")
    equal(error_code(record), INVALID_INPUT, "the code for an unsupported output extension")
    equal(
        message_of(record),
        "“.zzz” files are not supported as outputs for this algorithm",
        "the sentence for an unsupported output extension",
    )
    written = list(ctx.fixtures.root.glob("p19 output*"))
    require(not written, f"the refused destination still wrote {[path.name for path in written]}")
    return (
        "OUTPUT='p19 output.zzz' is FAILED(INVALID_INPUT) with the output widget's sentence and "
        "writes no file, not even the .zzz.gpkg QGIS would have appended"
    )


def p20_crs_mismatch_question(ctx: Context) -> str:
    require_processing()
    if "gdal:clipvectorbypolygon" not in {algorithm.id() for algorithm in processing_algorithms()}:
        raise Skip(
            "gdal:clipvectorbypolygon is not registered here, so the CRS question cannot be "
            "measured"
        )

    # 夹具里的要素没有几何，而 GDAL 的裁剪需要真的几何：这里直接在工程里搭一个点图层（4326）
    # 和一个面掩膜（32633），两者的 CRS 不一致就是本用例要测的那件事。
    # The fixtures carry no geometry, and GDAL's clip needs real geometry: this case builds a
    # point layer (4326) and a polygon mask (32633) in the project, and their mismatched CRSes are
    # what the case measures.
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer

    points = QgsVectorLayer("Point?crs=EPSG:4326&field=id:integer", "p20 points", "memory")
    provider = points.dataProvider()
    for index in range(3):
        feature = QgsFeature(points.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(index), float(index))))
        feature["id"] = index
        provider.addFeature(feature)
    points.updateExtents()
    QgsProject.instance().addMapLayer(points)

    # 掩膜在 32633 里盖住这些点变换后的位置（约 166000E），所以裁剪会真的留下一层要素。
    # The mask covers where those points land in 32633 (about 166000E), so the clip really keeps
    # features.
    mask = QgsVectorLayer("Polygon?crs=EPSG:32633&field=id:integer", "p20 mask", "memory")
    provider = mask.dataProvider()
    feature = QgsFeature(mask.fields())
    feature.setGeometry(
        QgsGeometry.fromPolygonXY(
            [
                [
                    QgsPointXY(-1_000_000, -1_000_000),
                    QgsPointXY(-1_000_000, 1_000_000),
                    QgsPointXY(1_000_000, 1_000_000),
                    QgsPointXY(1_000_000, -1_000_000),
                ]
            ]
        )
    )
    feature["id"] = 1
    provider.addFeature(feature)
    mask.updateExtents()
    QgsProject.instance().addMapLayer(mask)
    before = project_layer_ids()

    # 桌面版在参数 CRS 不一致时会问“要继续吗”，默认按钮是“否”；operation 用参数回答同一句。
    # The desktop asks "do you want to continue?" when the parameters' CRSes differ, with "No" as
    # the default button; the operation answers that question with a parameter.
    parameters = {"INPUT": points.id(), "MASK": mask.id()}
    refused = run_algorithm(ctx, "gdal:clipvectorbypolygon", parameters)
    equal(refused.status, ExecutionStatus.FAILED, "a CRS mismatch with no answer")
    equal(error_code(refused), INVALID_INPUT, "the code for a CRS mismatch with no answer")
    equal(
        message_of(refused),
        "Parameters do not all use the same CRS. This can cause unexpected results.\n"
        "Do you want to continue?",
        "the CRS question",
    )
    equal(project_layer_ids(), before, "the layers after the refusal")

    continued = ctx.run(
        "processing.run_algorithm",
        {
            "algorithm_id": "gdal:clipvectorbypolygon",
            "parameters": parameters,
            "continue_on_crs_mismatch": True,
        },
    )
    equal(continued.status, ExecutionStatus.SUCCEEDED, f"the answered run: {continued.error}")
    output = outputs_of(continued)["OUTPUT"]
    require(isinstance(output, str), f"the continued run's OUTPUT is {output!r}, not a path")
    require(Path(output).is_file(), f"the continued run wrote no file at {output!r}")
    written = QgsVectorLayer(output, "p20 written", "ogr")
    require(written.isValid(), f"the continued run's output {output!r} is not a readable layer")

    # 开关关着 = 桌面版不问。持久化的是字符串 'false'，只有按 ProcessingConfig 的读法才算关。
    # The switch off means the desktop does not ask.  The persisted value is the string 'false',
    # and it is off only when read the way ProcessingConfig reads it.
    from qgis.core import QgsSettings

    settings = QgsSettings()
    switch = "Processing/Configuration/WARN_UNMATCHING_CRS"
    settings.setValue(switch, "false")
    settings.sync()
    try:
        unchecked = run_algorithm(ctx, "gdal:clipvectorbypolygon", parameters)
        equal(unchecked.status, ExecutionStatus.SUCCEEDED, f"the switch-off run: {unchecked.error}")
        unchecked_output = outputs_of(unchecked)["OUTPUT"]
        require(
            Path(unchecked_output).is_file(),
            f"the switch-off run wrote no file at {unchecked_output!r}",
        )
    finally:
        settings.remove(switch)
        settings.sync()

    return (
        "gdal:clipvectorbypolygon on a 4326 input and a 32633 mask is FAILED(INVALID_INPUT) "
        f"with the desktop's question ({message_of(refused)!r}) and adds nothing; the same call "
        f"with continue_on_crs_mismatch=true writes a readable GeoPackage "
        f"({written.featureCount()} feature(s)); with the desktop's warning switch persisted as "
        "'false' the same parameters also run, so the question follows the desktop's switch and "
        "its answer is a parameter"
    )


# ---------------------------------------------------------- QML 导出 / QML export


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
        "Point, Line and Polygon layers all report the same 18 base categories in the PRD's fixed "
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
        equal(categories, RASTER_CATEGORY_NAMES, f"{name}: the ten categories")
        for required in ("Labeling", "AttributeTable"):
            require(required in categories, f"{name}: {required} is missing from {categories}")
    return (
        "a one-band and a three-band raster both report the same ten categories, including "
        "Labeling and AttributeTable, in the PRD's fixed order"
    )


def s03_invalid_categories(ctx: Context) -> str:
    vector_id = sub_layer(ctx, "s03 polygon", fixtures.PARCELS_LAYER)
    raster_id = raster_layer(ctx, "s03 raster")
    requests = {
        "an empty list": (vector_id, []),
        "a repeated category": (vector_id, ["Symbology", "Symbology"]),
        "an unknown name": (vector_id, ["p5NotAStyleCategory"]),
        "a vector-only category on a raster": (raster_id, ["Symbology3D"]),
        "an empty list on a raster": (raster_id, []),
    }
    for label, (layer_id, categories) in requests.items():
        record = ctx.run("layer.get_qml", {"layer_id": layer_id, "categories": categories})
        equal(record.status, ExecutionStatus.FAILED, f"{label}: status")
        equal(error_code(record), INVALID_INPUT, f"{label}: code")
        require(record.result is None, f"{label}: a half-finished export was returned")
    return (
        "an empty list, a repeated category, an unknown name and a vector-only category on a "
        "raster are all FAILED(INVALID_INPUT) with no partial XML"
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
        "(message, ok): charter 7/E2); the failure branch stays covered by "
        "tests/test_operations_layer.py's fake"
    )


# ------------------------------------------------ 候选渲染器 / renderer candidates


def a01_vector_candidates(ctx: Context) -> str:
    from qgis.core import QgsApplication

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
        equal(lists[geometry], sorted(set(native)), f"{geometry}: the live per-layer registry list")
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
    from qgis.core import QgsRasterRendererRegistry

    native = QgsRasterRendererRegistry().renderersList()
    equal(sorted(native), RASTER_RENDERER_TYPES, "the eight built-in raster renderers")
    equal(sorted(set(native)), sorted(native), "the raster registry repeats itself")

    layer_id = raster_layer(ctx, "a02 raster", ctx.fixtures.raster3)
    record = ctx.run("layer.list_available_renderers", {"layer_id": layer_id})
    equal(
        record.status,
        ExecutionStatus.SUCCEEDED,
        f"layer.list_available_renderers: {record.error}",
    )
    types = result_of(record)["renderer_types"]
    equal(types, sorted(set(native)), "the raster list against QgsRasterRendererRegistry")
    equal(len(types), 8, "the registered raster renderer count")
    return (
        "a raster layer reports the same eight types QgsRasterRendererRegistry().renderersList() "
        "holds for the whole environment, not a per-layer selection "
        f"({', '.join(types)})"
    )


# 由 A03 注册进应用级注册表，所以这个实例要活得比用例久：原生注册表接管所有权，Python 必须留住
# 包装对象。
# Registered into the application registry by A03, so the instance outlives the case: the native
# registry takes ownership, and Python must keep its wrapper alive.
_REGISTERED_RENDERER_METADATA: list[Any] = []


def a03_third_party_candidate(ctx: Context) -> str:
    from qgis.core import (
        QgsApplication,
        QgsMarkerSymbol,
        QgsRendererAbstractMetadata,
        QgsSingleSymbolRenderer,
    )

    layer_id = cities_layer(ctx, "a03 point")
    before = result_of(ctx.run("layer.list_available_renderers", {"layer_id": layer_id}))[
        "renderer_types"
    ]
    # 设置器只实现五种矢量渲染器，它设不了的那几个照样是候选——这就是“不按设置器过滤”。
    # The setter implements five vector types; the registered ones it cannot set are candidates all
    # the same, which is what "not filtered by the setter" means.
    for name in ("heatmapRenderer", "pointCluster", "pointDisplacement"):
        require(name in before, f"{name} is missing from the candidates: {before}")

    class AcceptanceMetadata(QgsRendererAbstractMetadata):
        def __init__(self) -> None:
            super().__init__("p5AcceptanceRenderer", "P5 acceptance renderer")

        def createRenderer(self, element):  # noqa: N802 - the native spelling
            return QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple({"color": "#8866cc"}))

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
    require(sorted(after) == after, f"the list is no longer sorted: {after}")
    record = set_renderer(ctx, layer_id, {"type": "p5AcceptanceRenderer"})
    equal(record.status, ExecutionStatus.FAILED, "the setter accepted a type it does not implement")
    equal(error_code(record), INVALID_INPUT, "the code for a type the setter does not implement")
    return (
        "the candidates include the three registered Point types the setter does not implement, "
        "and a renderer registered into the application registry during this run appears in the "
        "next call even though layer.set_renderer refuses it with INVALID_INPUT"
    )


def a03b_raster_third_party(ctx: Context) -> str:
    from qgis.core import QgsApplication

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
        "be observed from Python (charter 7/E3): the raster half of A03 is unverifiable here and "
        "A02 measures the built-in eight instead"
    )


def a04_bad_layers(ctx: Context) -> str:
    from qgis.core import QgsProject, QgsVectorLayer

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
    for label, layer_id in (("a vector table", table.id()), ("an invalid layer", invalid.id())):
        for operation in (*operations, "layer.get_qml"):
            record = ctx.run(operation, {"layer_id": layer_id})
            equal(record.status, ExecutionStatus.FAILED, f"{operation} on {label}")
            equal(error_code(record), INVALID_LAYER, f"{operation}: the code for {label}")
    return (
        "an unknown id is LAYER_NOT_FOUND on all three renderer/style reads; a valid vector table "
        "(no geometry) and a registered but invalid layer are INVALID_LAYER on all four"
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
        equal(types, sorted(types), f"{label}: ascending order")
        equal(len(types), len(set(types)), f"{label}: duplicates in {types}")
        second = ctx.run("layer.list_available_renderers", {"layer_id": layer_id})
        equal(result_of(second), result, f"{label}: a repeated call differs")
        counts[label] = len(types)
    return (
        f"deduplicated, ascending and identical on a repeated call, with only renderer_types in "
        f"the result (vector {counts['vector']} types, raster {counts['raster']} types)"
    )


def a06_get_renderer(ctx: Context) -> str:
    from qgis.core import QgsHeatmapRenderer

    vector_id = cities_layer(ctx, "a06 point")
    raster_id = raster_layer(ctx, "a06 raster", ctx.fixtures.raster3)

    for layer_id in (vector_id, raster_id):
        record = ctx.run("layer.get_renderer", {"layer_id": layer_id})
        equal(record.status, ExecutionStatus.SUCCEEDED, f"{layer_id}: status")
        result = result_of(record)
        equal(sorted(result), ["layer_id", "renderer_type"], "the result fields")
        equal(result["renderer_type"], project_layer(layer_id).renderer().type(), "the live type")

    # 设置器造不出来的渲染器照样能读，它的配置从 QML 导出里取。
    # A renderer the setter cannot build is still read, and its configuration comes from the QML
    # export.
    project_layer(vector_id).setRenderer(QgsHeatmapRenderer())
    equal(installed_renderer(ctx, vector_id), "heatmapRenderer", "a type the setter cannot build")
    qml = qml_of(ctx, vector_id, ["Symbology"])
    require("heatmapRenderer" in qml, "the heatmap configuration is not in the QML export")
    return (
        "both layer kinds report exactly the live renderer().type() with layer_id and "
        "renderer_type only; a heatmapRenderer the setter cannot build reads back as "
        "'heatmapRenderer' and its full configuration is in the Symbology QML"
    )


# ------------------------------------------- 矢量渲染器设置 / vector setters


def v01_null_symbol(ctx: Context) -> str:
    from qgis.core import QgsProject

    layer_id = sub_layer(ctx, "v01 polygon", fixtures.PARCELS_LAYER)
    before = result_of(ctx.run("layer.get_info", {"layer_id": layer_id}))
    QgsProject.instance().setDirty(False)

    record = set_renderer(ctx, layer_id, {"type": "nullSymbol"})
    equal(record.status, ExecutionStatus.SUCCEEDED, f"layer.set_renderer failed: {record.error}")
    equal(result_of(record)["renderer_type"], "nullSymbol", "the reported type")
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
        "nullSymbol installed as the layer's own renderer, visibility and layer-tree presence "
        "unchanged, and the project is dirty afterwards although QgsVectorLayer.setRenderer alone "
        "does not mark it (measured)"
    )


def v02_single_symbol(ctx: Context) -> str:
    from qgis.core import QgsSymbol

    cases = (
        (
            "point",
            cities_layer(ctx, "v02 point"),
            {"type": "marker", "color": "#6CAC83", "size_mm": 3.5},
        ),
        (
            "line",
            sub_layer(ctx, "v02 line", fixtures.ROADS_LAYER),
            {"type": "line", "color": "#D95843", "width_mm": 0.8},
        ),
        (
            "polygon",
            sub_layer(ctx, "v02 polygon", fixtures.PARCELS_LAYER),
            {
                "type": "fill",
                "color": "#275E40",
                "outline_color": "#111111",
                "outline_width_mm": 0.5,
            },
        ),
    )
    for label, layer_id, symbol in cases:
        record = set_renderer(ctx, layer_id, {"type": "singleSymbol", "symbol": symbol})
        equal(record.status, ExecutionStatus.SUCCEEDED, f"{label}: {record.error}")
        equal(result_of(record)["renderer_type"], "singleSymbol", f"{label}: the reported type")
        renderer = project_layer(layer_id).renderer()
        equal(renderer.type(), "singleSymbol", f"{label}: the native renderer")
        installed = renderer.symbol()
        families = {"marker": QgsSymbol.Marker, "line": QgsSymbol.Line, "fill": QgsSymbol.Fill}
        equal(installed.type(), families[symbol["type"]], f"{label}: the symbol family")
        equal(installed.color().name(), symbol["color"].lower(), f"{label}: the colour")
        if symbol["type"] == "marker":
            equal(installed.size(), symbol["size_mm"], f"{label}: the marker size in mm")
        elif symbol["type"] == "line":
            equal(installed.width(), symbol["width_mm"], f"{label}: the line width in mm")
        else:
            layer_zero = installed.symbolLayer(0)
            equal(layer_zero.strokeColor().name(), symbol["outline_color"].lower(), "outline")
            equal(layer_zero.strokeWidth(), symbol["outline_width_mm"], "outline width")
        equal(installed_renderer(ctx, layer_id), "singleSymbol", f"{label}: layer.get_renderer")
    return (
        "singleSymbol installed on Point, Line and Polygon with the declared colour, marker size, "
        "line width and fill outline read back from the native symbol, each agreeing with "
        "layer.get_renderer"
    )


def v03_categorized(ctx: Context) -> str:
    layer_id = sub_layer(ctx, "v03 polygon", fixtures.PARCELS_LAYER)
    config = {
        "type": "categorizedSymbol",
        "field": "landuse",
        "categories": [
            {"value": "forest", "label": "Forest", "symbol": {"type": "fill", "color": "#398F59"}},
            {"value": "water", "label": "Water", "symbol": {"type": "fill", "color": "#419ACB"}},
        ],
    }
    record = set_renderer(ctx, layer_id, config)
    equal(record.status, ExecutionStatus.SUCCEEDED, f"layer.set_renderer failed: {record.error}")
    equal(result_of(record)["renderer_type"], "categorizedSymbol", "the reported type")

    renderer = project_layer(layer_id).renderer()
    equal(renderer.type(), "categorizedSymbol", "the native renderer")
    equal(renderer.classAttribute(), "landuse", "the classification field")
    categories = renderer.categories()
    equal([entry.value() for entry in categories], ["forest", "water"], "the category order")
    equal([entry.label() for entry in categories], ["Forest", "Water"], "the labels")
    equal(
        [entry.symbol().color().name() for entry in categories],
        ["#398f59", "#419acb"],
        "the category colours",
    )
    field = project_layer(layer_id).fields().indexFromName("landuse")
    require(field >= 0, "the field the renderer classifies by is gone")
    values = {feature.attribute(field) for feature in project_layer(layer_id).getFeatures()}
    require(
        values <= {entry.value() for entry in categories},
        f"the layer holds values the renderer has no category for: {values}",
    )

    qml = qml_of(ctx, layer_id, ["Symbology"])
    for label in ("Forest", "Water"):
        require(f'label="{label}"' in qml, f"{label} is missing from the QML export")

    again = set_renderer(ctx, layer_id, config)
    equal(again.status, ExecutionStatus.SUCCEEDED, f"the second identical call: {again.error}")
    equal(
        [entry.value() for entry in project_layer(layer_id).renderer().categories()],
        ["forest", "water"],
        "the category order after setting the same configuration twice",
    )
    return (
        "categorizedSymbol keeps the given order, values, labels and colours on the native "
        "QgsCategorizedSymbolRenderer, classifies by the declared field, covers every value the "
        "layer holds, and appears with both labels in the QML; a second identical call is stable"
    )


def v04_graduated(ctx: Context) -> str:
    layer_id = sub_layer(ctx, "v04 polygon", fixtures.PARCELS_LAYER)
    ranges = [
        {
            "lower": 0,
            "upper": 100,
            "label": "Small",
            "symbol": {"type": "fill", "color": "#CFE5B5"},
        },
        {
            "lower": 100,
            "upper": 200,
            "label": "Large",
            "symbol": {"type": "fill", "color": "#468B46"},
        },
    ]
    record = set_renderer(
        ctx, layer_id, {"type": "graduatedSymbol", "field": "area", "ranges": ranges}
    )
    equal(record.status, ExecutionStatus.SUCCEEDED, f"layer.set_renderer failed: {record.error}")
    equal(result_of(record)["renderer_type"], "graduatedSymbol", "the reported type")

    renderer = project_layer(layer_id).renderer()
    equal(renderer.type(), "graduatedSymbol", "the native renderer")
    equal(renderer.classAttribute(), "area", "the classification field")
    installed = renderer.ranges()
    equal([entry.lowerValue() for entry in installed], [0.0, 100.0], "the lower bounds")
    equal([entry.upperValue() for entry in installed], [100.0, 200.0], "the upper bounds")
    equal([entry.label() for entry in installed], ["Small", "Large"], "the labels")
    equal(
        [entry.symbol().color().name() for entry in installed],
        ["#cfe5b5", "#468b46"],
        "the range colours",
    )

    overlapping = [
        ranges[0],
        {
            "lower": 50,
            "upper": 200,
            "label": "Overlap",
            "symbol": {"type": "fill", "color": "#111111"},
        },
    ]
    refused = set_renderer(
        ctx, layer_id, {"type": "graduatedSymbol", "field": "area", "ranges": overlapping}
    )
    equal(refused.status, ExecutionStatus.FAILED, "overlapping ranges")
    equal(error_code(refused), INVALID_INPUT, "the code for overlapping ranges")
    equal(installed_renderer(ctx, layer_id), "graduatedSymbol", "the renderer after the refusal")
    equal(
        [entry.label() for entry in project_layer(layer_id).renderer().ranges()],
        ["Small", "Large"],
        "the ranges after the refusal",
    )
    return (
        "graduatedSymbol installed both ranges with their bounds, labels and colours on the native "
        "QgsGraduatedSymbolRenderer; ranges that overlap are FAILED(INVALID_INPUT) and the "
        "installed ranges are unchanged"
    )


def v05_rules(ctx: Context) -> str:
    layer_id = sub_layer(ctx, "v05 line", fixtures.ROADS_LAYER)
    rules = [
        {
            "label": "First",
            "filter": '"road_id" = 7',
            "symbol": {"type": "line", "color": "#D95843", "width_mm": 0.8},
        },
        {
            "label": "Second",
            "filter": '"road_id" > 100',
            "symbol": {"type": "line", "color": "#888888", "width_mm": 0.25},
        },
        {
            "label": "Third",
            "filter": '"road_id" < 0',
            "symbol": {"type": "line", "color": "#00A0FF", "width_mm": 0.5},
        },
    ]
    record = set_renderer(ctx, layer_id, {"type": "RuleRenderer", "rules": rules})
    equal(record.status, ExecutionStatus.SUCCEEDED, f"layer.set_renderer failed: {record.error}")
    equal(result_of(record)["renderer_type"], "RuleRenderer", "the reported type")

    renderer = project_layer(layer_id).renderer()
    equal(renderer.type(), "RuleRenderer", "the native renderer")
    children = renderer.rootRule().children()
    equal(len(children), 3, "the number of flat rules")
    equal([rule.label() for rule in children], ["First", "Second", "Third"], "the rule order")
    equal(
        [rule.filterExpression() for rule in children],
        ['"road_id" = 7', '"road_id" > 100', '"road_id" < 0'],
        "the expressions as given",
    )
    equal([rule.isElse() for rule in children], [False, False, False], "the ELSE flags")
    require(all(rule.active() for rule in children), "a rule was installed inactive")
    equal(
        [rule.symbol().color().name() for rule in children],
        ["#d95843", "#888888", "#00a0ff"],
        "the rule colours",
    )
    qml = qml_of(ctx, layer_id, ["Symbology"])
    for label in ("First", "Second", "Third"):
        require(f'label="{label}"' in qml, f"{label} is missing from the QML export")
    return (
        "three flat rules installed on the native QgsRuleBasedRenderer in the given order, with "
        "their filter expressions, symbols and active flags read back from rootRule(), and all "
        "three labels in the Symbology QML"
    )


def v06_else_rule(ctx: Context) -> str:
    layer_id = sub_layer(ctx, "v06 line", fixtures.ROADS_LAYER)
    rules = [
        {
            "label": "Matched",
            "filter": '"road_id" = 7',
            "symbol": {"type": "line", "color": "#D95843", "width_mm": 0.8},
        },
        {
            "label": "Everything else",
            "else_rule": True,
            "symbol": {"type": "line", "color": "#888888", "width_mm": 0.25},
        },
    ]
    record = set_renderer(ctx, layer_id, {"type": "RuleRenderer", "rules": rules})
    equal(record.status, ExecutionStatus.SUCCEEDED, f"layer.set_renderer failed: {record.error}")

    renderer = project_layer(layer_id).renderer()
    children = renderer.rootRule().children()
    equal(len(children), 2, "the rules including the implicit catch-all the PRD forbids")
    equal([rule.label() for rule in children], ["Matched", "Everything else"], "the rule order")
    equal([rule.isElse() for rule in children], [False, True], "the ELSE flags")
    equal(children[-1].filterExpression(), "ELSE", "the native spelling of an else rule")
    require(all(rule.active() for rule in children), "a rule was installed inactive")

    qml = qml_of(ctx, layer_id, ["Symbology"])
    require('label="Everything else"' in qml, "the else rule is missing from the QML export")
    require(qml.count("<rule ") == 2, f"the QML export holds {qml.count('<rule ')} rule elements")
    return (
        "one ordinary rule and one ELSE rule are the only two children of rootRule(); the ELSE is "
        "last and is the last child in the QML as well, so no implicit all-matching default rule "
        "was appended"
    )


def v07_refusals(ctx: Context) -> str:
    layer_id = sub_layer(ctx, "v07 line", fixtures.ROADS_LAYER)
    kept = {
        "type": "RuleRenderer",
        "rules": [
            {
                "label": "Keep",
                "filter": '"road_id" = 7',
                "symbol": {"type": "line", "color": "#D95843", "width_mm": 0.8},
            }
        ],
    }
    equal(set_renderer(ctx, layer_id, kept).status, ExecutionStatus.SUCCEEDED, "the kept renderer")

    symbol = {"type": "line", "color": "#111111", "width_mm": 0.3}
    refused = {
        "an invalid expression": {
            "type": "RuleRenderer",
            "rules": [{"label": "Bad", "filter": '"road_id" = = 1', "symbol": symbol}],
        },
        "two ELSE rules": {
            "type": "RuleRenderer",
            "rules": [
                {"label": "One", "filter": "1 = 1", "symbol": symbol},
                {"label": "A", "else_rule": True, "symbol": symbol},
                {"label": "B", "else_rule": True, "symbol": symbol},
            ],
        },
        "an ELSE that is not last": {
            "type": "RuleRenderer",
            "rules": [
                {"label": "A", "else_rule": True, "symbol": symbol},
                {"label": "B", "filter": "1 = 1", "symbol": symbol},
            ],
        },
        "an ELSE with a filter": {
            "type": "RuleRenderer",
            "rules": [
                {"label": "A", "filter": "1 = 1", "symbol": symbol},
                {"label": "B", "else_rule": True, "filter": "1 = 1", "symbol": symbol},
            ],
        },
    }
    for label, config in refused.items():
        record = set_renderer(ctx, layer_id, config)
        equal(record.status, ExecutionStatus.FAILED, f"{label}: status")
        equal(error_code(record), INVALID_INPUT, f"{label}: code")
        equal(installed_renderer(ctx, layer_id), "RuleRenderer", f"{label}: the renderer type")
        equal(
            [rule.label() for rule in project_layer(layer_id).renderer().rootRule().children()],
            ["Keep"],
            f"{label}: the rules the old renderer still has",
        )
    return (
        "an unparseable expression, two ELSE rules, an ELSE that is not last and an ELSE that "
        "carries a filter are all FAILED(INVALID_INPUT), and the previously installed "
        "RuleRenderer keeps its single rule every time"
    )


def v08_unsupported(ctx: Context) -> str:
    layer_id = cities_layer(ctx, "v08 point")
    kept = {"type": "singleSymbol", "symbol": {"type": "marker", "color": "#6CAC83"}}
    equal(set_renderer(ctx, layer_id, kept).status, ExecutionStatus.SUCCEEDED, "the kept renderer")

    unsupported = (
        "heatmapRenderer",
        "pointCluster",
        "pointDisplacement",
        "mergedFeatureRenderer",
        "invertedPolygonRenderer",
        "25dRenderer",
        "embeddedSymbol",
        "paletted",
        "hillshade",
    )
    for name in unsupported:
        record = set_renderer(ctx, layer_id, {"type": name})
        equal(record.status, ExecutionStatus.FAILED, f"{name}: status")
        equal(error_code(record), INVALID_INPUT, f"{name}: code")

    mismatch = set_renderer(
        ctx, layer_id, {"type": "singleSymbol", "symbol": {"type": "fill", "color": "#FFFFFF"}}
    )
    equal(mismatch.status, ExecutionStatus.FAILED, "a fill symbol on a point layer")
    equal(error_code(mismatch), INVALID_INPUT, "the code for a symbol that does not match")
    equal(installed_renderer(ctx, layer_id), "singleSymbol", "the renderer after the refusals")

    candidates = result_of(ctx.run("layer.list_available_renderers", {"layer_id": layer_id}))[
        "renderer_types"
    ]
    for name in ("heatmapRenderer", "pointCluster", "pointDisplacement"):
        require(name in candidates, f"{name} is no longer a candidate: {candidates}")
    return (
        f"{len(unsupported)} renderer types the setter does not implement and a fill symbol on a "
        "point layer are all FAILED(INVALID_INPUT) with the previous renderer untouched, while "
        "layer.list_available_renderers still lists the unsupported types as candidates"
    )


# ------------------------------------------- 栅格渲染器设置 / raster setters


def r01_single_band_gray(ctx: Context) -> str:
    from qgis.core import QgsContrastEnhancement, QgsSingleBandGrayRenderer

    layer_id = raster_layer(ctx, "r01 raster", ctx.fixtures.raster3)
    record = set_renderer(
        ctx, layer_id, {"type": "singlebandgray", "band": 2, "min": 1.0, "max": 200.0}
    )
    equal(record.status, ExecutionStatus.SUCCEEDED, f"layer.set_renderer failed: {record.error}")
    equal(result_of(record)["renderer_type"], "singlebandgray", "the reported type")

    renderer = project_layer(layer_id).renderer()
    equal(renderer.type(), "singlebandgray", "the native renderer")
    require(isinstance(renderer, QgsSingleBandGrayRenderer), f"the native class: {type(renderer)}")
    equal(renderer.inputBand(), 2, "the band the renderer reads")
    enhancement = renderer.contrastEnhancement()
    require(enhancement is not None, "the renderer carries no contrast enhancement")
    equal(enhancement.minimumValue(), 1.0, "the minimum of the stretch")
    equal(enhancement.maximumValue(), 200.0, "the maximum of the stretch")
    equal(
        enhancement.contrastEnhancementAlgorithm(),
        QgsContrastEnhancement.StretchToMinimumMaximum,
        "the contrast algorithm",
    )
    equal(installed_renderer(ctx, layer_id), "singlebandgray", "layer.get_renderer")
    return (
        "singlebandgray installed band 2 with a StretchToMinimumMaximum enhancement of 1.0..200.0 "
        "on the native QgsSingleBandGrayRenderer (a Byte band, so the stretch is stored as given)"
    )


def r02_multiband_color(ctx: Context) -> str:
    layer_id = raster_layer(ctx, "r02 raster", ctx.fixtures.raster3)
    record = set_renderer(
        ctx, layer_id, {"type": "multibandcolor", "red_band": 3, "green_band": 2, "blue_band": 2}
    )
    equal(record.status, ExecutionStatus.SUCCEEDED, f"layer.set_renderer failed: {record.error}")
    equal(result_of(record)["renderer_type"], "multibandcolor", "the reported type")

    renderer = project_layer(layer_id).renderer()
    equal(renderer.type(), "multibandcolor", "the native renderer")
    equal(renderer.redBand(), 3, "the red band")
    equal(renderer.greenBand(), 2, "the green band")
    equal(renderer.blueBand(), 2, "the blue band, which repeats the green one")
    return (
        "multibandcolor installed red=3, green=2, blue=2 on the native renderer: distinct bands "
        "map to the channels and naming the same band twice is accepted"
    )


def r03_pseudo_color(ctx: Context) -> str:
    from qgis.core import QgsColorRampShader

    layer_id = raster_layer(ctx, "r03 raster", ctx.fixtures.raster3)
    stops = [{"value": 0, "color": "#2665B8"}, {"value": 10, "color": "#F2E9A4"}]
    record = set_renderer(
        ctx,
        layer_id,
        {"type": "singlebandpseudocolor", "band": 2, "interpolation": "discrete", "stops": stops},
    )
    equal(record.status, ExecutionStatus.SUCCEEDED, f"layer.set_renderer failed: {record.error}")
    equal(result_of(record)["renderer_type"], "singlebandpseudocolor", "the reported type")

    renderer = project_layer(layer_id).renderer()
    equal(renderer.type(), "singlebandpseudocolor", "the native renderer")
    equal(renderer.inputBand(), 2, "the band the renderer reads")
    function = renderer.shader().rasterShaderFunction()
    equal(function.colorRampType(), QgsColorRampShader.Discrete, "the ramp type for 'discrete'")
    items = function.colorRampItemList()
    equal([item.value for item in items], [0.0, 10.0], "the stop values")
    equal([item.color.name() for item in items], ["#2665b8", "#f2e9a4"], "the stop colours")

    linear = set_renderer(
        ctx,
        layer_id,
        {"type": "singlebandpseudocolor", "band": 2, "interpolation": "linear", "stops": stops},
    )
    equal(linear.status, ExecutionStatus.SUCCEEDED, f"the linear ramp: {linear.error}")
    equal(
        project_layer(layer_id).renderer().shader().rasterShaderFunction().colorRampType(),
        QgsColorRampShader.Interpolated,
        "the ramp type for 'linear'",
    )

    exact = set_renderer(
        ctx,
        layer_id,
        {"type": "singlebandpseudocolor", "band": 2, "interpolation": "exact", "stops": stops},
    )
    equal(exact.status, ExecutionStatus.SUCCEEDED, f"the exact ramp: {exact.error}")
    equal(
        project_layer(layer_id).renderer().shader().rasterShaderFunction().colorRampType(),
        QgsColorRampShader.Exact,
        "the ramp type for 'exact'",
    )

    inverted = set_renderer(
        ctx,
        layer_id,
        {
            "type": "singlebandpseudocolor",
            "band": 2,
            "stops": [{"value": 10, "color": "#2665B8"}, {"value": 0, "color": "#F2E9A4"}],
        },
    )
    equal(inverted.status, ExecutionStatus.FAILED, "stops in decreasing order")
    equal(error_code(inverted), INVALID_INPUT, "the code for stops in decreasing order")
    equal(installed_renderer(ctx, layer_id), "singlebandpseudocolor", "the renderer after that")
    return (
        "singlebandpseudocolor installed band 2 with the two stops and colors in the native "
        "shader, "
        "'discrete' maps to Discrete, 'linear' to Interpolated and 'exact' to Exact, and stops in "
        "decreasing order "
        "are FAILED(INVALID_INPUT) with the installed renderer unchanged"
    )


def r04_single_color(ctx: Context) -> str:
    layer_id = raster_layer(ctx, "r04 raster", ctx.fixtures.raster3)
    record = set_renderer(ctx, layer_id, {"type": "singlecolor", "band": 3, "color": "#CC6633"})
    equal(record.status, ExecutionStatus.SUCCEEDED, f"layer.set_renderer failed: {record.error}")
    equal(result_of(record)["renderer_type"], "singlecolor", "the reported type")

    renderer = project_layer(layer_id).renderer()
    equal(renderer.type(), "singlecolor", "the native renderer type")
    equal(renderer.inputBand(), 3, "the band the renderer reads")
    equal(renderer.color().name(), "#cc6633", "the installed colour")
    equal(installed_renderer(ctx, layer_id), "singlecolor", "layer.get_renderer")
    return (
        "singlecolor installed band 3 and #CC6633 on the native 3.38+ raster renderer type "
        "(distinct from singlebandcolordata), read back from the object itself"
    )


def r05_raster_refusals(ctx: Context) -> str:
    layer_id = raster_layer(ctx, "r05 raster", ctx.fixtures.raster3)
    kept = {"type": "singlebandgray", "band": 1, "min": 0.0, "max": 10.0}
    equal(set_renderer(ctx, layer_id, kept).status, ExecutionStatus.SUCCEEDED, "the kept renderer")

    stops = [{"value": 0, "color": "#2665B8"}, {"value": 10, "color": "#F2E9A4"}]
    refused = {
        "band 0": {"type": "singlebandgray", "band": 0, "min": 0.0, "max": 1.0},
        "band 4 of a three-band raster": {
            "type": "singlebandgray",
            "band": 4,
            "min": 0.0,
            "max": 1.0,
        },
        "min equal to max": {"type": "singlebandgray", "band": 1, "min": 5.0, "max": 5.0},
        "a NaN bound": {"type": "singlebandgray", "band": 1, "min": float("nan"), "max": 1.0},
        "an infinite bound": {"type": "singlebandgray", "band": 1, "min": 0.0, "max": float("inf")},
        "a band 9 in multibandcolor": {
            "type": "multibandcolor",
            "red_band": 1,
            "green_band": 2,
            "blue_band": 9,
        },
        "a short colour": {"type": "singlecolor", "band": 1, "color": "#12345"},
        "a non-hex colour": {"type": "singlecolor", "band": 1, "color": "#GGGGGG"},
        "stops in decreasing order": {
            "type": "singlebandpseudocolor",
            "band": 1,
            "stops": [stops[1], stops[0]],
        },
        "an infinite stop": {
            "type": "singlebandpseudocolor",
            "band": 1,
            "stops": [{"value": float("inf"), "color": "#2665B8"}, stops[1]],
        },
        "a single stop": {"type": "singlebandpseudocolor", "band": 1, "stops": [stops[0]]},
    }
    for label, config in refused.items():
        record = set_renderer(ctx, layer_id, config)
        equal(record.status, ExecutionStatus.FAILED, f"{label}: status")
        equal(error_code(record), INVALID_INPUT, f"{label}: code")
        equal(installed_renderer(ctx, layer_id), "singlebandgray", f"{label}: the renderer type")
        renderer = project_layer(layer_id).renderer()
        equal(renderer.inputBand(), 1, f"{label}: the band of the old renderer")
    return (
        f"{len(refused)} invalid raster configurations (band bounds, min>=max, non-finite "
        "numbers, illegal colours, stop order, a single stop) are all FAILED(INVALID_INPUT), and "
        "the previously installed singlebandgray renderer keeps its band and settings each time"
    )


CASES = (
    ("AT-01", at_01_duplicate_registration),
    ("AT-02", at_02_invalid_input),
    ("AT-03", at_03_run_sync),
    ("AT-04", at_04_submit_layer_add),
    ("AT-05", at_05_run_sync_refuses_background),
    ("AT-06", at_06_submit_field_sum),
    ("AT-07", at_07_worker_exception),
    ("AT-08", at_08_cancel_running),
    ("AT-08B", at_08b_cancel_scheduled),
    ("AT-08C", at_08c_concurrency_limit),
    ("AT-09", at_09_cancel_queued),
    ("AT-10", at_10_cancel_finalizing),
    ("AT-11", at_11_project_replaced),
    ("AT-12", at_12_layer_removed_while_running),
    ("AT-13", at_13_unload_with_running_worker),
    ("AT-14", at_14_submission_refused),
    ("AT-15", at_15_stateful_replace_refused),
    ("AT-16", at_16_stateful_replace),
    ("AT-17", at_17_save_refuses_existing),
    ("AT-18", at_18_corrupt_project),
    ("AT-18B", at_18b_project_with_unavailable_layer),
    ("AT-19", at_19_path_policy),
    ("AT-20", at_20_invalid_source),
    ("AT-21", at_21_layer_without_tree_node),
    ("AT-21B", at_21b_group_hides_layer),
    ("AT-22", at_22_visibility_idempotent),
    ("AT-23", at_23_finalize_or_output_fails),
    ("AT-24", at_24_retention),
    ("AT-25", at_25_three_sessions),
    ("AT-26", at_26_wrong_thread),
    ("AT-27", at_27_pydantic_v2),
    ("AT-27-v1", at_27_v1_half),
    ("AT-28", at_28_layer_add_sub_layer),
    ("AT-29", at_29_open_accepts_a_source_outside_any_root),
    ("AT-30", at_30_open_refuses_a_remote_source_before_reading),
    ("AT-31", at_31_open_refuses_an_encoded_or_spaced_datasource),
    ("AT-32", at_32_open_keeps_a_source_whose_directory_is_gone),
    ("AT-33", at_33_open_refuses_an_oversized_member_before_unpacking_it),
    ("AT-34", at_34_unreadable_member_is_a_project_read_failure),
    ("AT-35", at_35_open_refuses_a_deeply_nested_project_before_reading_it),
    ("AT-36", at_36_open_refuses_an_unclosed_datasource_project_in_milliseconds),
    ("AT-37", at_37_open_refuses_a_qgz_over_the_total_budget),
    ("AT-38", at_38_open_refuses_a_tag_spam_project_in_milliseconds),
    ("AT-39", at_39_open_refuses_a_datasource_hidden_by_an_attribute),
    ("AT-40", at_40_open_reads_a_project_with_labelled_layers),
    ("AT-41", at_41_open_refuses_depth_written_inside_cdata_comments_or_attributes),
    ("AT-42", at_42_open_refuses_an_archive_whose_sibling_member_is_encrypted),
    ("AT-43", at_43_open_reads_a_project_with_a_deep_group_tree),
    ("AT-44", at_44_field_sum_refuses_a_non_finite_value),
    ("AT-45", at_45_open_refuses_a_source_that_a_child_element_carries_in_its_tail),
    ("AT-46", at_46_open_refuses_a_project_that_declares_xml_entities),
    ("AT-47", at_47_open_reads_projects_the_old_scan_called_remote),
    ("AT-48", at_48_open_refuses_a_source_split_across_child_elements),
    ("AT-49", at_49_open_reads_a_project_whose_source_is_inside_a_vsi_directory),
    ("AT-50", at_50_open_refuses_a_subdataset_that_names_a_remote_file),
    ("AT-51", at_51_open_reads_a_project_whose_layer_is_a_local_subdataset),
    ("P01", p01_list_algorithms),
    ("P02", p02_no_algorithm_allow_list),
    ("P03", p03_algorithm_info_fields),
    ("P04", p04_unknown_algorithm),
    ("P05", p05_unknown_key),
    ("P06", p06_buffer_memory_output),
    ("P07", p07_array_input),
    ("P08", p08_scalar_output),
    ("P09", p09_multiple_outputs),
    ("P10", p10_json_values),
    ("P12", p12_input_path_policy),
    ("P13", p13_destination_policy),
    ("P14", p14_output_ownership),
    ("P15", p15_non_json_output),
    ("P16", p16_submit_cancel_queued),
    ("P17", p17_gui_output_defaults),
    ("P18", p18_parameter_value_refused),
    ("P19", p19_output_extension_refused),
    ("P20", p20_crs_mismatch_question),
    ("S01", s01_vector_categories),
    ("S02", s02_raster_categories),
    ("S03", s03_invalid_categories),
    ("S04", s04_symbology_only),
    ("S05", s05_multiple_and_raster_pipe),
    ("S06", s06_export_failure),
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
    ("V06", v06_else_rule),
    ("V07", v07_refusals),
    ("V08", v08_unsupported),
    ("R01", r01_single_band_gray),
    ("R02", r02_multiband_color),
    ("R03", r03_pseudo_color),
    ("R04", r04_single_color),
    ("R05", r05_raster_refusals),
)
