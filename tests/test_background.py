"""Background work end to end: the engine, the QgsTask adapter, and the one real task.

The adapter's own accounting is checked in ``test_qgs_task``; here the question is
what a caller sees: a field sum that really goes through a snapshot, a cancel that
is only reported once QGIS confirms it, a result that never lands in a replaced
project, and a shutdown that stops what this plugin started (PRD 8.4, 8.5, 9.1-9.3).

The layer is a fake, the way ``test_operations_layer`` fakes it: the real provider
and the real ``QgsTaskManager`` are the acceptance run's business.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from pydantic import ValidationError
from test_qgs_task import Counting, DoneResult, Harness

from qgis_agent_plugin.core.config import Settings
from qgis_agent_plugin.core.contracts import BackgroundOperationMixin, BaseQuery, OperationContext
from qgis_agent_plugin.core.models import (
    ENGINE_CLOSING,
    INTERNAL_ERROR,
    INVALID_FIELD_TYPE,
    INVALID_LAYER,
    LAYER_NOT_FOUND,
    OUTPUT_VALIDATION_FAILED,
    SNAPSHOT_LIMIT_EXCEEDED,
    STALE_PROJECT,
    UNSUPPORTED_EXECUTION_MODE,
    ExecutionRecord,
    ExecutionStatus,
    FrameworkError,
    OperationKind,
    Params,
)
from qgis_agent_plugin.core.registry import OperationRegistry
from qgis_agent_plugin.operations.layer import (
    FieldSum,
    FieldValues,
    LayerSumNumericField,
    SumNumericFieldParams,
)

QUEUED = ExecutionStatus.QUEUED
SCHEDULED = ExecutionStatus.SCHEDULED
RUNNING = ExecutionStatus.RUNNING
CANCEL_REQUESTED = ExecutionStatus.CANCEL_REQUESTED
FINALIZING = ExecutionStatus.FINALIZING
SUCCEEDED = ExecutionStatus.SUCCEEDED
FAILED = ExecutionStatus.FAILED
CANCELLED = ExecutionStatus.CANCELLED

SUM = LayerSumNumericField.name
FIELD = "population"

# The qgis.core.NULL singleton, reduced to a value the fake layer can hand out.
NULL = object()


# --- the fake QGIS -----------------------------------------------------------


class FakeField:
    def __init__(self, name: str, numeric: bool = True) -> None:
        self.name = name
        self.numeric = numeric

    def isNumeric(self) -> bool:  # noqa: N802 - the QGIS name
        return self.numeric


class FakeFields:
    def __init__(self, fields: list[FakeField]) -> None:
        self._fields = fields

    def indexFromName(self, name: str) -> int:  # noqa: N802 - the QGIS name
        for index, field in enumerate(self._fields):
            if field.name == name:
                return index
        return -1

    def at(self, index: int) -> FakeField:
        return self._fields[index]


class FakeFeature:
    def __init__(self, values: tuple[Any, ...]) -> None:
        self._values = values

    def __getitem__(self, index: int) -> Any:
        return self._values[index]


class FakeVectorLayer:
    """Enough of ``QgsVectorLayer`` for one field read."""

    def __init__(
        self,
        rows: list[tuple[Any, ...]],
        fields: list[FakeField] | None = None,
        valid: bool = True,
    ) -> None:
        self.rows = rows
        self._fields = FakeFields(fields if fields is not None else [FakeField(FIELD)])
        self._valid = valid

    def isValid(self) -> bool:  # noqa: N802 - the QGIS name
        return self._valid

    def fields(self) -> FakeFields:
        return self._fields

    def getFeatures(self) -> Any:  # noqa: N802 - the QGIS name
        return iter([FakeFeature(row) for row in self.rows])


@pytest.fixture(autouse=True)
def fake_qgis(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """A ``qgis.core`` that carries only what the sum operation imports."""

    core = types.ModuleType("qgis.core")
    core.QgsVectorLayer = FakeVectorLayer  # type: ignore[attr-defined]
    core.NULL = NULL  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "qgis", types.ModuleType("qgis"))
    monkeypatch.setitem(sys.modules, "qgis.core", core)
    sys.modules["qgis"].core = core  # type: ignore[attr-defined]
    return core


# --- helpers -----------------------------------------------------------------


def make_layer(rows: list[tuple[Any, ...]], **overrides: Any) -> FakeVectorLayer:
    return FakeVectorLayer(rows, **overrides)


def harness_with(layer: Any = None, settings: Settings | None = None) -> Harness:
    harness = Harness(LayerSumNumericField(), settings=settings)
    if layer is not None:
        harness.project.layers["layer-1"] = layer
    return harness


def submit_sum(harness: Harness, layer_id: str = "layer-1") -> ExecutionRecord:
    return harness.submit(SUM, {"layer_id": layer_id, "field_name": FIELD})


def settled(harness: Harness, submitted: ExecutionRecord) -> ExecutionRecord:
    """Run the worker and its completion, the way QGIS would.

    A call whose snapshot preparation failed never got a task, so there is nothing
    to run: its record already says why.
    """

    task = harness.tasks[-1] if harness.tasks else None
    if task is not None and task.execution_id == submitted.execution_id:
        harness.run(task)
    return harness.record(submitted)


# --- what the caller sees ----------------------------------------------------


def test_a_field_sum_is_computed_from_a_copied_snapshot() -> None:
    harness = harness_with(make_layer([(10,), (None,), (5,)]))
    submitted = submit_sum(harness)

    # PRD 6.5: submit only accepts and queues; the snapshot is built a turn later.
    assert submitted.status is QUEUED
    assert harness.record(submitted).status is SCHEDULED
    snapshot = harness.tasks[0].snapshot
    assert isinstance(snapshot, FieldValues)
    assert snapshot.values == (10.0, 5.0)
    assert snapshot.skipped_null == 1
    assert isinstance(snapshot.values, tuple)

    record = settled(harness, submitted)
    assert record.status is SUCCEEDED
    assert record.result == {
        "layer_id": "layer-1",
        "field_name": FIELD,
        "count": 2,
        "skipped_null": 1,
        "total": 15.0,
        "snapshot_project_epoch": 0,
    }
    assert record.progress == 100


def test_a_null_that_arrives_as_the_qgis_null_value_is_skipped() -> None:
    harness = harness_with(make_layer([(7,), (NULL,), (1,)]))

    record = settled(harness, submit_sum(harness))

    # PRD 7.10: a null is counted as skipped, not summed as zero.
    assert record.status is SUCCEEDED
    assert record.result is not None
    assert record.result["count"] == 2
    assert record.result["skipped_null"] == 1
    assert record.result["total"] == 8.0


def test_every_value_null_is_an_empty_sum_not_a_failure() -> None:
    harness = harness_with(make_layer([(None,), (None,)]))

    record = settled(harness, submit_sum(harness))

    assert record.status is SUCCEEDED
    assert record.result is not None
    assert record.result["count"] == 0
    assert record.result["total"] == 0.0
    assert record.result["skipped_null"] == 2


def test_the_task_depends_on_the_layer_it_reads() -> None:
    layer = make_layer([(1,)])
    harness = harness_with(layer)

    submit_sum(harness)

    # PRD 8.4 / AT-12: QGIS cancels the task when its dependent layer goes away.
    assert harness.tasks[0].dependent_layers == [layer]
    assert LayerSumNumericField().dependent_layer_ids(
        SumNumericFieldParams(layer_id="layer-1", field_name=FIELD)
    ) == ["layer-1"]


# --- refusals that happen before any task exists -----------------------------


def test_a_synchronous_field_sum_is_unsupported_and_leaves_no_record() -> None:
    harness = harness_with(make_layer([(1,)]))

    with pytest.raises(FrameworkError) as caught:
        harness.engine.run_sync(SUM, {"layer_id": "layer-1", "field_name": FIELD})

    # AT-05: a background operation cannot be run synchronously, and the refused
    # call never became an execution.
    assert caught.value.code == UNSUPPORTED_EXECUTION_MODE
    assert harness.engine._store.ids(set(ExecutionStatus)) == []
    assert harness.tasks == []


@pytest.mark.parametrize("value", [None, 1, "", True])
def test_the_parameters_accept_only_a_plain_layer_and_field_name(value: Any) -> None:
    with pytest.raises(ValidationError):
        SumNumericFieldParams.model_validate({"layer_id": value, "field_name": FIELD})
    with pytest.raises(ValidationError):
        SumNumericFieldParams.model_validate({"layer_id": "a", "field_name": value})


def test_an_undeclared_parameter_is_refused() -> None:
    with pytest.raises(ValidationError):
        SumNumericFieldParams.model_validate({"layer_id": "a", "field_name": FIELD, "limit": 10})


def test_the_operation_registers_as_a_background_query() -> None:
    registry = OperationRegistry()
    registry.register(LayerSumNumericField())

    spec = registry.get_spec(SUM)

    assert spec.kind is OperationKind.QUERY
    assert spec.destructive is False
    assert LayerSumNumericField.strategy.value == "background"


# --- snapshot refusals -------------------------------------------------------


def test_an_unknown_layer_is_layer_not_found() -> None:
    harness = harness_with()

    record = settled(harness, submit_sum(harness))

    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == LAYER_NOT_FOUND


def test_a_layer_that_is_not_a_valid_vector_is_invalid_layer() -> None:
    harness = harness_with(make_layer([(1,)], valid=False))

    record = settled(harness, submit_sum(harness))

    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == INVALID_LAYER


def test_a_raster_layer_is_invalid_layer() -> None:
    harness = Harness(LayerSumNumericField())
    harness.project.layers["layer-1"] = object()

    record = settled(harness, submit_sum(harness))

    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == INVALID_LAYER


def test_a_field_the_layer_does_not_have_is_invalid_field_type() -> None:
    harness = harness_with(make_layer([(1,)], fields=[FakeField("other")]))

    record = settled(harness, submit_sum(harness))

    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == INVALID_FIELD_TYPE


def test_a_field_that_is_not_numeric_is_invalid_field_type() -> None:
    harness = harness_with(make_layer([("a",)], fields=[FakeField(FIELD, numeric=False)]))

    record = settled(harness, submit_sum(harness))

    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == INVALID_FIELD_TYPE


def test_more_features_than_the_limit_is_refused_not_truncated() -> None:
    harness = harness_with(
        make_layer([(1,), (2,), (3,)]), settings=Settings(max_snapshot_features=2)
    )

    record = settled(harness, submit_sum(harness))

    # PRD 7.10: a partial sum that looks complete is worse than a refusal.
    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == SNAPSHOT_LIMIT_EXCEEDED
    assert record.result is None
    assert harness.tasks == []


# --- cancellation ------------------------------------------------------------


def test_cancelling_a_scheduled_task_waits_for_qgis_to_confirm() -> None:
    harness = harness_with(make_layer([(1,)]))
    submitted = submit_sum(harness)

    receipt = harness.engine.cancel(submitted.execution_id)

    # AT-08B: the request is recorded and the task is asked to stop, but nothing
    # claims it has terminated yet.
    assert receipt.accepted is True
    assert receipt.status is CANCEL_REQUESTED
    assert harness.tasks[0].isCanceled() is True
    assert harness.record(submitted).status is CANCEL_REQUESTED

    harness.tasks[0].finished(False)  # QGIS confirms the termination

    assert harness.record(submitted).status is CANCELLED
    assert harness.record(submitted).result is None


def test_a_worker_that_finishes_after_a_cancel_reports_cancelled_without_a_result() -> None:
    harness = harness_with(make_layer([(4,)]))
    submitted = submit_sum(harness)
    harness.engine.cancel(submitted.execution_id)

    # PRD 8.2: ``run`` may still return True after the request; the result is dropped.
    harness.run(harness.tasks[0])

    record = harness.record(submitted)
    assert record.status is CANCELLED
    assert record.result is None
    assert record.error is None


def test_a_real_worker_failure_outranks_a_cancel_request() -> None:
    operation = Counting()
    operation.fail = True
    harness = Harness(operation)
    submitted = harness.submit(operation.name, {"amount": 2})
    harness.engine.cancel(submitted.execution_id)

    harness.run(harness.tasks[0])

    record = harness.record(submitted)
    # PRD 8.2: CANCEL_REQUESTED -> FAILED, keeping the diagnostic.
    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == INTERNAL_ERROR


def test_a_cancel_while_finalizing_is_refused() -> None:
    harness = harness_with(make_layer([(1,)]))
    submitted = submit_sum(harness)

    # Walk the record into FINALIZING through the store: the adapter's own step into
    # that state lasts one call and is not reachable from outside.
    store = harness.engine._store
    store.transition(submitted.execution_id, ExecutionStatus.RUNNING)
    store.transition(submitted.execution_id, FINALIZING)
    receipt = harness.engine.cancel(submitted.execution_id)

    assert receipt.accepted is False
    assert receipt.reason == "FINALIZING"


# --- the project generation --------------------------------------------------


def test_a_result_from_a_replaced_project_is_stale_project() -> None:
    harness = harness_with(make_layer([(1,)]))
    submitted = submit_sum(harness)

    harness.lifecycle.bump()  # what F6 does on aboutToBeCleared

    record = settled(harness, submitted)
    # PRD 9.1, AT-11: the old generation's numbers never reach the new project.
    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == STALE_PROJECT


def test_replacing_the_project_cancels_waiting_work_and_asks_the_tasks_to_stop() -> None:
    harness = harness_with(make_layer([(1,)]))
    first = submit_sum(harness)
    second = submit_sum(harness)
    waiting = submit_sum(harness)

    harness.lifecycle.bump()
    harness.engine.on_project_replaced()

    assert harness.record(waiting).status is CANCELLED
    assert harness.record(first).status is CANCEL_REQUESTED
    assert harness.record(second).status is CANCEL_REQUESTED
    assert [task.isCanceled() for task in harness.tasks] == [True, True]


def test_replacing_the_project_leaves_new_work_alone() -> None:
    harness = harness_with(make_layer([(1,)]))
    harness.lifecycle.bump()
    harness.engine.on_project_replaced()

    submitted = submit_sum(harness)

    # The engine stays open: only the old generation was dropped (PRD 9.2).
    assert harness.record(submitted).status is SCHEDULED
    assert harness.tasks[0].isCanceled() is False


# --- shutdown ----------------------------------------------------------------


def test_shutdown_stops_what_this_plugin_started_and_refuses_new_work() -> None:
    harness = harness_with(make_layer([(1,)]))
    first = submit_sum(harness)
    second = submit_sum(harness)
    waiting = submit_sum(harness)

    harness.engine.shutdown()

    # PRD 9.3.2: everything queued is cancelled, every live task is asked to stop.
    assert harness.record(waiting).status is CANCELLED
    assert harness.record(first).status is CANCEL_REQUESTED
    assert harness.record(second).status is CANCEL_REQUESTED
    assert [task.isCanceled() for task in harness.tasks] == [True, True]
    with pytest.raises(FrameworkError) as caught:
        harness.engine.submit(SUM, {"layer_id": "layer-1", "field_name": FIELD})
    assert caught.value.code == ENGINE_CLOSING


def test_shutdown_is_idempotent() -> None:
    harness = harness_with(make_layer([(1,)]))
    submit_sum(harness)

    harness.engine.shutdown()
    harness.engine.shutdown()

    assert harness.tasks[0].isCanceled() is True


# --- the finalize stage ------------------------------------------------------


class BadFinalize(Counting):
    """A worker that succeeds and a finalize that breaks."""

    name = "test.bad_finalize"
    description = "raise while the result is being applied"

    def finalize_main(self, ctx: OperationContext, request: Params, result: int) -> DoneResult:
        raise RuntimeError("finalize broke")


class LyingFinalize(Counting):
    """A finalize that returns something other than its declared output model."""

    name = "test.lying_finalize"
    description = "return an object instead of its output model"

    def finalize_main(self, ctx: OperationContext, request: Params, result: int) -> DoneResult:
        return object()  # type: ignore[return-value]


def test_a_finalize_that_raises_is_a_failed_record() -> None:
    harness = Harness(BadFinalize())
    submitted = harness.submit(BadFinalize.name, {"amount": 1})

    harness.run(harness.tasks[0])

    record = harness.record(submitted)
    # AT-23: a broken finalise may never look like a success.
    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == INTERNAL_ERROR
    assert record.result is None


def test_an_output_that_does_not_match_the_model_is_refused() -> None:
    harness = Harness(LyingFinalize())
    submitted = harness.submit(LyingFinalize.name, {"amount": 1})

    harness.run(harness.tasks[0])

    record = harness.record(submitted)
    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == OUTPUT_VALIDATION_FAILED
    assert record.result is None


# --- the worker's own contract ------------------------------------------------


def test_the_worker_gets_a_tuple_and_a_control_and_nothing_else() -> None:
    harness = harness_with(make_layer([(3.5,), (1.5,)]))

    submit_sum(harness)
    snapshot = harness.tasks[0].snapshot

    assert snapshot.values == (3.5, 1.5)
    assert not hasattr(snapshot, "mapLayer")
    result = LayerSumNumericField().execute_worker(snapshot, _NoCancel())
    # The declared worker result, before the main thread turns it into the output.
    assert isinstance(result, FieldSum)
    assert (result.count, result.total, result.skipped_null) == (2, 5.0, 0)


class _NoCancel:
    """A TaskControl that never cancels and records the progress it was given."""

    def __init__(self) -> None:
        self.progress: list[float] = []

    def is_cancelled(self) -> bool:
        return False

    def set_progress(self, value: float) -> None:
        self.progress.append(value)


def test_the_worker_stops_when_the_control_reports_a_cancel() -> None:
    class Cancelling(_NoCancel):
        def is_cancelled(self) -> bool:
            return True

    snapshot = FieldValues(
        layer_id="layer-1",
        field_name=FIELD,
        values=tuple(float(value) for value in range(1000)),
        skipped_null=0,
        project_epoch=0,
    )

    result = LayerSumNumericField().execute_worker(snapshot, Cancelling())

    # The partial numbers are handed back untouched; the adapter drops them.
    assert result.count == 9
    assert result.total == sum(range(9))


def test_the_worker_reports_progress_across_the_whole_range() -> None:
    snapshot = FieldValues(
        layer_id="layer-1",
        field_name=FIELD,
        values=tuple(1.0 for _ in range(400)),
        skipped_null=0,
        project_epoch=0,
    )
    control = _NoCancel()

    LayerSumNumericField().execute_worker(snapshot, control)

    assert control.progress[0] == 1.0
    assert control.progress[-1] == 100.0
    assert len(control.progress) <= 100


def test_the_finalize_refuses_a_snapshot_from_an_older_generation() -> None:
    harness = harness_with(make_layer([(1,)]))
    operation = LayerSumNumericField()
    request = SumNumericFieldParams(layer_id="layer-1", field_name=FIELD)
    snapshot = FieldValues(
        layer_id="layer-1", field_name=FIELD, values=(1.0,), skipped_null=0, project_epoch=0
    )
    result = operation.execute_worker(snapshot, _NoCancel())
    harness.lifecycle.bump()

    with pytest.raises(FrameworkError) as caught:
        operation.finalize_main(harness.engine._context(), request, result)

    # PRD 7.10: the operation checks the generation itself, not only the adapter.
    assert caught.value.code == STALE_PROJECT


def test_the_mixin_declares_all_three_stages() -> None:
    operation = LayerSumNumericField()

    assert isinstance(operation, BackgroundOperationMixin)
    assert isinstance(operation, BaseQuery)
    assert operation.kind is OperationKind.QUERY
    assert not hasattr(operation, "execute_main")
    assert callable(operation.prepare_main)
    assert callable(operation.execute_worker)
    assert callable(operation.finalize_main)


def test_the_field_sum_runs_state_by_state_with_a_running_record() -> None:
    harness = harness_with(make_layer([(1,)]))
    submitted = submit_sum(harness)

    # A progress report is what moves SCHEDULED to RUNNING on the main thread.
    harness.manager.report(harness.tasks[0], 50.0)

    assert harness.record(submitted).status is RUNNING
    assert harness.record(submitted).progress == 50.0
