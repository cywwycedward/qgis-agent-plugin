"""The PRD 12.2 acceptance cases, one function per AT id.

Each case returns one evidence line for the report and raises ``AssertionError``
when the expectation is not met, so a case can never pass by accident.  A case that
cannot be run on this machine raises ``Skip`` with the reason instead; nothing here
turns a skip into a pass (PRD 12.3).

Every case builds the state it needs through the plugin's own operations, against
the generated fixtures only (PRD 12.1: no user project is ever the subject of a
destructive case).
"""

from __future__ import annotations

import json
import threading
import time
from functools import partial
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
    INVALID_INPUT,
    INVALID_LAYER,
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
from qgis_agent_plugin.plugin import read_settings

LIVE_STATUSES = [
    ExecutionStatus.QUEUED,
    ExecutionStatus.PREPARING,
    ExecutionStatus.SCHEDULED,
    ExecutionStatus.RUNNING,
    ExecutionStatus.CANCEL_REQUESTED,
    ExecutionStatus.FINALIZING,
]

ALL_STATUSES = [*LIVE_STATUSES, *TERMINAL_STATUSES]


# --------------------------------------------------------------------------- helpers


def project_layers() -> dict[str, Any]:
    from qgis.core import QgsProject

    return QgsProject.instance().mapLayers()


def project_layer_ids() -> list[str]:
    return sorted(project_layers())


def reset_project(ctx: Context) -> None:
    """A clean project for a case's own setup, cleared directly, not by an operation."""

    from qgis.core import QgsProject

    QgsProject.instance().clear()
    pump(ctx.app, 0.05)


def cities_layer(ctx: Context, name: str = "cities") -> str:
    """A real vector layer added through the plugin's own ``layer.add``."""

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
    """The code of the ``FrameworkError`` ``call`` raised, or a failed assertion."""

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


# ------------------------------------------------------------------------- the cases


def at_01_duplicate_registration(ctx: Context) -> str:
    registry = ctx.registry
    names_before = sorted(spec.name for spec in registry.list_specs())
    equal(len(names_before), 9, "operations registered at start-up")
    original = registry.get("project.get_info")

    code = refused(lambda: registry.register(DuplicateProjectInfo()))

    equal(code, DUPLICATE_OPERATION, "the refusal code")
    require(registry.get("project.get_info") is original, "the original registration was replaced")
    equal(
        sorted(spec.name for spec in registry.list_specs()), names_before, "names after the refusal"
    )
    return f"{code}; project.get_info still {type(original).__name__}; the nine specs are unchanged"


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
    equal(PROBE_LOG.starts, labels, "the order the waiting tasks started in")
    equal(
        [ctx.status(execution_id) for execution_id in ids],
        [ExecutionStatus.SUCCEEDED] * len(ids),
        "the final status of every task",
    )
    return (
        f"four submissions, never more than {peak} active, two QUEUED, and the waiting ones "
        f"started in submission order ({', '.join(PROBE_LOG.starts)})"
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
        """What PRD 12.2 AT-14 says to simulate: ``addTask`` returns 0."""

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
    """Both sides of PRD 7.3: a corrupt QGZ is refused, a valid one is read."""

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
    settings = read_settings()
    equal(settings.allowed_data_roots, (ctx.fixtures.root,), "the allowed data roots")
    equal(settings.allowed_project_roots, (ctx.fixtures.root,), "the allowed project roots")

    outside = ctx.fixtures.outside / "outside.gpkg"
    outside.write_bytes(ctx.fixtures.cities.read_bytes())
    sources = {
        "a file outside the allowed roots": str(outside),
        "an https URI": "https://example.com/data.gpkg",
        "a GDAL virtual file system": "/vsizip/" + str(ctx.fixtures.cities).replace("\\", "/"),
    }
    if ctx.fixtures.escape is None:
        raise Skip(
            "could not create the directory junction this machine needs for the "
            "'link that leaves the allowed roots' sub-case"
        )
    sources["a junction that leaves the allowed roots"] = str(ctx.fixtures.escape / "outside.gpkg")

    layers_before = project_layer_ids()
    for label, source in sources.items():
        record = ctx.run("layer.add", {"kind": "vector", "source_path": source, "name": "at19"})
        equal(record.status, ExecutionStatus.FAILED, f"layer.add from {label}")
        equal(error_code(record), PATH_NOT_ALLOWED, f"the code for {label}")

    record = ctx.run("project.save", {"path": str(ctx.fixtures.outside / "at19.qgs")})
    equal(record.status, ExecutionStatus.FAILED, "project.save outside the allowed roots")
    equal(error_code(record), PATH_NOT_ALLOWED, "the code for a save outside the roots")
    require(
        not (ctx.fixtures.outside / "at19.qgs").exists(), "a file was written outside the roots"
    )
    equal(project_layer_ids(), layers_before, "the layers after the refused calls")
    return (
        "PATH_NOT_ALLOWED for a file outside the roots, an https URI, a /vsi path and a junction "
        "that resolves out, and for a save outside the roots; nothing was loaded or written"
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

    # The TTL half needs 1800 seconds of retention time, so it runs on an injected clock
    # against the same store (PRD 12.1 allows the pure-Python layer to do this).
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
        equal(len(plugin.registry.list_specs()), 9, f"round {number}: the nine operations")
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
        "three loads and unloads: nine operations per round, exactly one epoch bump per project "
        "clear, no console handle left, a fresh engine and registry per round, and a clear after "
        "every session is gone left all three epochs at 1 (no repeated signal connection); each "
        "unloaded engine refuses new work"
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
    equal(len(specs), 9, "the nine specs")
    for spec in specs:
        equal(spec.pydantic_major, 2, f"{spec.name}: pydantic_major")
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
        f"pydantic {pydantic.VERSION}: the nine specs export and round-trip JSON with "
        "pydantic_major=2, StrictBool refuses the string 'true', and the terminal record with its "
        "result round-trips. The v1 half of AT-27 is a separate, skipped row"
    )


def at_27_v1_half(ctx: Context) -> str:
    raise Skip(
        "pydantic 1.x is not installed here and this build does not support it (charter "
        "decision 5: the DTOs are written against v2 and plugin.require_supported_pydantic "
        "refuses any other major), so the v1 half of AT-27 cannot be run"
    )


def at_28_layer_add_sub_layer(ctx: Context) -> str:
    """PRD 7.6: one named table in a GeoPackage, reached through the OGR encoder."""

    from qgis.core import QgsProject

    reset_project(ctx)
    source = ctx.fixtures.spaced  # its own name contains a space on purpose
    layer_id = ctx.add_layer(source, "at28 roads", sub_layer=fixtures.ROADS_LAYER)
    layer = QgsProject.instance().mapLayer(layer_id)
    if layer is None:
        raise AssertionError("layer.add reported SUCCEEDED but registered no layer")

    equal(layer.providerType(), "ogr", "the provider of a sub-layer add")
    equal(layer.isValid(), True, "the layer is usable")
    # The GeoPackage holds five cities points and one roads line, so the count is
    # what proves the encoder selected the named table and not the first one.
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


def at_29_open_refuses_a_source_outside_the_roots(ctx: Context) -> str:
    """A project file may not smuggle in data the path policy refuses (PRD 10.2)."""

    record = ctx.run("project.open", {"path": str(ctx.fixtures.leak), "discard_current": True})

    equal(
        record.status,
        ExecutionStatus.FAILED,
        "project.open on a project whose layer source is outside the roots",
    )
    equal(error_code(record), PATH_NOT_ALLOWED, "the code for an outside data source")
    names = [layer.name() for layer in project_layers().values()]
    require("leaked" not in names, f"the outside layer is still loaded: {names}")
    equal(len(project_layers()), 0, "the layers left in the project")
    reset_project(ctx)
    return (
        "the QGZ itself is inside allowed_project_roots, its only layer names a GeoPackage in a "
        "sibling directory: FAILED(PATH_NOT_ALLOWED), the layer was removed from the project, "
        "and no layer stayed registered"
    )


def at_30_open_refuses_a_remote_source_before_reading(ctx: Context) -> str:
    """A project file may not cost a network round trip before it is checked."""

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
        # The file's own datasource text is refused before read(), so neither the
        # HTTP retry nor the SMB connect happens on the main thread.
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
    """A UTF-8-only text scan can be walked past with an encoding or a space (A3 #1)."""

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
        # Neither form may reach read(): 10 s of GDAL retries is what the audit measured
        # when the scan missed the datasource it could not see.
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
    """A moved-away source directory is AT-18B, not a breach of the roots (A3 #2)."""

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
        "does not exist but its path is inside allowed_data_roots: the layer was not "
        "removed and the read was not turned into PATH_NOT_ALLOWED"
    )


def at_33_open_refuses_an_oversized_member_before_unpacking_it(ctx: Context) -> str:
    """A tiny QGZ may hold a member that expands to 300 MB, on the main thread (A3 #3)."""

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
        f"{elapsed * 1000:.0f} ms, before the member was unpacked; the old project kept its layer"
    )


def at_34_unreadable_member_is_a_project_read_failure(ctx: Context) -> str:
    """A QGZ the scanner cannot unpack is the file's problem, not the plugin's (A3 #4)."""

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
    """A deeply nested project crashes ``read()``'s parser, so it never reaches it (A4 #2)."""

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
    """984 KB of unclosed tags once cost the main thread 661 s; it is linear now (A4 #1)."""

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
    """The budget is the sum of the members, not each member on its own (A4 #3)."""

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
        f"FAILED(PATH_NOT_ALLOWED) in {elapsed * 1000:.0f} ms with no member unpacked; "
        "the old per-member check accepted this shape and read every member (measured "
        "peak 200 MB for 100 members)"
    )


def at_38_open_refuses_a_tag_spam_project_in_milliseconds(ctx: Context) -> str:
    """16 MB of XML elements is refused by count, not walked element by element (A4 #1)."""

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
    require("XML elements" in error.message, f"the message: {error.message!r}")
    require(elapsed < 0.5, f"the {on_disk} byte project took {elapsed:.2f}s")
    equal(project_layer_ids(), before, "the project's layers after the refusal")
    equal(QgsProject.instance().fileName(), file_before, "the project's file after the refusal")
    reset_project(ctx)
    return (
        f"a {on_disk} byte project of {fixtures.TAG_SPAM_PAIRS} empty elements: "
        f"FAILED(PROJECT_OPEN_FAILED) in {elapsed * 1000:.0f} ms, one bytes.count over "
        "the bytes; a per-element walk would have held the main thread for seconds"
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
    ("AT-29", at_29_open_refuses_a_source_outside_the_roots),
    ("AT-30", at_30_open_refuses_a_remote_source_before_reading),
    ("AT-31", at_31_open_refuses_an_encoded_or_spaced_datasource),
    ("AT-32", at_32_open_keeps_a_source_whose_directory_is_gone),
    ("AT-33", at_33_open_refuses_an_oversized_member_before_unpacking_it),
    ("AT-34", at_34_unreadable_member_is_a_project_read_failure),
    ("AT-35", at_35_open_refuses_a_deeply_nested_project_before_reading_it),
    ("AT-36", at_36_open_refuses_an_unclosed_datasource_project_in_milliseconds),
    ("AT-37", at_37_open_refuses_a_qgz_over_the_total_budget),
    ("AT-38", at_38_open_refuses_a_tag_spam_project_in_milliseconds),
)
