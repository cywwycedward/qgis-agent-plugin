# 静态分析器自己的解释器里没有 qgis/pydantic，所以本文件只关掉“找不到模块”这一类报告；能不能
# 真的加载由 QGIS 解释器的验收跑证明。
# The analyzer's own interpreter has no qgis/pydantic, so this file turns off only the
# missing-module report; whether it really loads is proven by the QGIS interpreter's run.
# pyright: reportMissingImports=false

"""验收用例：引擎、执行与会话。

注册时的拒绝、控制面失败与失败记录的分界、排队与取消、工程代数、卸载之后仍在跑的任务、
保留期，以及只认创建线程的那条规则。

The acceptance cases for the engine, executions and the session: refusals at registration, the
line between a control-plane failure and a failed record, queueing and cancellation, the
project epoch, tasks still running after an unload, retention, and the rule that only the
creating thread may call in.
"""

from __future__ import annotations

import json
import threading
from functools import partial
from typing import Any

import fixtures  # pyright: ignore[reportMissingImports]
import pydantic  # pyright: ignore[reportMissingImports]
from harness import (  # pyright: ignore[reportMissingImports]
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
)
from helpers import (
    ALL_STATUSES,
    LIVE_STATUSES,
    all_record_ids,
    cities_layer,
    error_code,
    manager_task_count,
    project_layer,
    project_layer_ids,
    project_layers,
    refused,
    reset_project,
    result_of,
)
from pydantic import ValidationError  # pyright: ignore[reportMissingImports]

from qgis_agent_plugin.core.config import Settings  # pyright: ignore[reportMissingImports]
from qgis_agent_plugin.core.context import ProjectLifecycle  # pyright: ignore[reportMissingImports]
from qgis_agent_plugin.core.engine import OperationEngine  # pyright: ignore[reportMissingImports]
from qgis_agent_plugin.core.execution_store import TERMINAL_STATUSES  # pyright: ignore[reportMissingImports]
from qgis_agent_plugin.core.models import (  # pyright: ignore[reportMissingImports]
    DUPLICATE_OPERATION,
    ENGINE_CLOSING,
    EXECUTION_NOT_FOUND,
    INTERNAL_ERROR,
    INVALID_INPUT,
    OUTPUT_VALIDATION_FAILED,
    STALE_PROJECT,
    TASK_SUBMISSION_FAILED,
    UNSUPPORTED_EXECUTION_MODE,
    WRONG_THREAD,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionStrategy,
    FrameworkError,
    OperationSpec,
)
from qgis_agent_plugin.core.registry import OperationRegistry  # pyright: ignore[reportMissingImports]
from qgis_agent_plugin.core.runners.qgs_task import OperationTaskRunner, _dependent_layers  # pyright: ignore[reportMissingImports]
from qgis_agent_plugin.operations import register_all  # pyright: ignore[reportMissingImports]
from qgis_agent_plugin.operations.layer import (  # pyright: ignore[reportMissingImports]
    LayerSetVisibilityParams,
    LayerSumNumericField,
    LayerSumNumericFieldParams,
)


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
            "sub_layer": fixtures.CITIES_LAYER,
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
    equal(values["selected_only"], False, "no selection, so the whole layer")
    equal(values["snapshot_project_epoch"], ctx.lifecycle.current_epoch, "the snapshot epoch")
    return (
        f"QgsTask added (id {watch.added[0]}) and deleted by QGIS (id {watch.deleted[0]}), "
        f"status {record.status}, progress readable ({during.progress} while live, 100 at the "
        f"end); pop sum {values['total']} over {values['count']} values, "
        f"{values['skipped_null']} nulls — matches the baseline, selected_only=false"
    )


def at_06b_sum_uses_the_selection_only_when_asked(ctx: Context) -> str:
    """选择集只在 ``selected_only`` 为真时才算：面板上那个开关默认不勾。

    A selection counts only when ``selected_only`` is set: that panel switch starts
    unchecked.
    """

    layer_id = cities_layer(ctx, "at06b cities")
    layer = project_layer(layer_id)
    layer.selectByExpression("pop > 50")
    equal(layer.selectedFeatureCount(), 2, "the features the expression selected")

    whole = ctx.submit("layer.sum_numeric_field", {"layer_id": layer_id, "field_name": "pop"})
    first = ctx.wait_terminal(whole.execution_id)
    equal(first.status, ExecutionStatus.SUCCEEDED, f"the field sum: {first.error}")
    equal(result_of(first)["selected_only"], False, "the default is the whole layer")
    equal(result_of(first)["total"], fixtures.POP_TOTAL, "the whole layer's sum")

    selected = ctx.submit(
        "layer.sum_numeric_field",
        {"layer_id": layer_id, "field_name": "pop", "selected_only": True},
    )
    record = ctx.wait_terminal(selected.execution_id)
    equal(
        record.status, ExecutionStatus.SUCCEEDED, f"the field sum over a selection: {record.error}"
    )
    values = result_of(record)
    equal(values["selected_only"], True, "the record says the selection was used")
    equal(values["count"], 2, "the summed values are the selected ones")
    equal(values["total"], 160.0, "the sum of pop over the selection")
    equal(values["skipped_null"], 0, "the selection holds no null")

    layer.removeSelection()
    again = ctx.submit(
        "layer.sum_numeric_field",
        {"layer_id": layer_id, "field_name": "pop", "selected_only": True},
    )
    second = ctx.wait_terminal(again.execution_id)
    equal(second.status, ExecutionStatus.SUCCEEDED, "the sum after clearing the selection")
    equal(result_of(second)["total"], 0.0, "an empty selection sums nothing")
    return (
        "2 of 5 cities selected: the default call is the whole layer (200.0, selected_only=false); "
        "selected_only=true gives count=2, total=160.0; after removeSelection() the same call "
        "sums the empty selection (0.0)"
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
    equal(limit, 2, "max_active_background (2 by default)")
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
    first = ctx.submit(
        "layer.add",
        {
            "kind": "vector",
            "source_path": source,
            "name": "at09 first",
            "sub_layer": fixtures.CITIES_LAYER,
        },
    )
    second = ctx.submit(
        "layer.add",
        {
            "kind": "vector",
            "source_path": source,
            "name": "at09 second",
            "sub_layer": fixtures.CITIES_LAYER,
        },
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
    from qgis.core import QgsProject  # pyright: ignore[reportMissingImports]

    ctx.register_probes()
    reset_project(ctx)
    layer_id = cities_layer(ctx, "at12 cities")
    request = LayerSumNumericFieldParams(layer_id=layer_id, field_name="pop")

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
        "a manager whose addTask() returns 0 (the shape of a refused submission): "
        "FAILED(TASK_SUBMISSION_FAILED), one record and none left dangling"
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
    equal(ctx.engine._settings.max_terminal_records, 200, "max_terminal_records (200 by default)")
    equal(ctx.engine._settings.terminal_ttl_seconds, 1800, "terminal_ttl_seconds (1800 by default)")

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
        from qgis.core import QgsProject  # pyright: ignore[reportMissingImports]

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

    from qgis.core import QgsProject  # pyright: ignore[reportMissingImports]

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
        "pydantic 1.x is not installed here and this build does not support it: the DTOs are "
        "written against v2 and plugin.require_supported_pydantic refuses any other major, so the "
        "v1 half of AT-27 cannot be run"
    )


ENGINE_CASES = (
    ("AT-01", at_01_duplicate_registration),
    ("AT-02", at_02_invalid_input),
    ("AT-03", at_03_run_sync),
    ("AT-04", at_04_submit_layer_add),
    ("AT-05", at_05_run_sync_refuses_background),
    ("AT-06", at_06_submit_field_sum),
    ("AT-06B", at_06b_sum_uses_the_selection_only_when_asked),
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
    ("AT-23", at_23_finalize_or_output_fails),
    ("AT-24", at_24_retention),
    ("AT-25", at_25_three_sessions),
    ("AT-26", at_26_wrong_thread),
    ("AT-27", at_27_pydantic_v2),
    ("AT-27-v1", at_27_v1_half),
)
