"""后台执行走完整条链路：引擎、QgsTask 适配器，以及真正跑起来的那个任务。

适配器自己的账目在 ``test_qgs_task`` 里查；这里问的是调用者看到什么：一次真的经过快照的
字段求和、只有 QGIS 确认后才报出的取消、落到被替换工程里就作废的结果，以及关停时只停掉
本插件自己启动的东西。图层是伪造的，跟 ``test_operations_layer`` 一样：真的 provider 与
真的 ``QgsTaskManager`` 留给验收测试。

Background work end to end: the engine, the QgsTask adapter, and the one real task. The
adapter's own accounting is checked in ``test_qgs_task``; here the question is what a
caller sees: a field sum that really goes through a snapshot, a cancel that is only
reported once QGIS confirms it, a result that never lands in a replaced project, and a
shutdown that stops what this plugin started. The layer is a fake, the way
``test_operations_layer`` fakes it: the real provider and the real ``QgsTaskManager`` are
the acceptance run's business.
"""

from __future__ import annotations

import json
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
    LayerSumNumericFieldParams,
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

# qgis.core.NULL 缩成伪造图层能交出的值 / the qgis.core.NULL singleton as a plain value
NULL = object()


# --- 伪造的 QGIS / the fake QGIS ----------------------------------------------


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


class FakeFeatureRequest:
    """``QgsFeatureRequest`` 只留求和快照建请求所需的部分，并记下收到的裁剪。

    Enough of ``QgsFeatureRequest`` for the sum snapshot to build its request,
    remembering the trim it was handed.
    """

    NoGeometry = 1

    def __init__(self) -> None:
        self.flags = 0
        self.attribute_indexes: list[int] = []

    def setFlags(self, flags: int) -> FakeFeatureRequest:  # noqa: N802 - the QGIS name
        self.flags = flags
        return self

    def setSubsetOfAttributes(self, indexes: list[int]) -> FakeFeatureRequest:  # noqa: N802
        self.attribute_indexes = list(indexes)
        return self


class FakeFeature:
    def __init__(self, values: tuple[Any, ...]) -> None:
        self._values = values

    def __getitem__(self, index: int) -> Any:
        return self._values[index]


class FakeVectorLayer:
    """``QgsVectorLayer`` 只留一次字段读取所需的部分。

    Enough of ``QgsVectorLayer`` for one field read.
    """

    def __init__(
        self,
        rows: list[tuple[Any, ...]],
        fields: list[FakeField] | None = None,
        valid: bool = True,
        selected_rows: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self.rows = rows
        self._fields = FakeFields(fields if fields is not None else [FakeField(FIELD)])
        self._valid = valid
        # 选择集：空列表就是“没有选中任何要素”。
        # The selection: an empty list means nothing is selected.
        self.selected_rows = list(selected_rows or [])
        # 最后一次读请求，快照裁剪那条用例查它 / the last request; the trim test checks it
        self.last_request: Any = None

    def isValid(self) -> bool:  # noqa: N802 - the QGIS name
        return self._valid

    def fields(self) -> FakeFields:
        return self._fields

    def getFeatures(self, request: Any = None) -> Any:  # noqa: N802 - the QGIS name
        self.last_request = request
        return iter([FakeFeature(row) for row in self.rows])

    def selectedFeatureCount(self) -> int:  # noqa: N802 - the QGIS name
        return len(self.selected_rows)

    def getSelectedFeatures(self, request: Any = None) -> Any:  # noqa: N802 - the QGIS name
        self.last_request = request
        return iter([FakeFeature(row) for row in self.selected_rows])


@pytest.fixture(autouse=True)
def fake_qgis(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """只带求和操作会 import 的那几个名字的 ``qgis.core``。

    A ``qgis.core`` that carries only what the sum operation imports.
    """

    core = types.ModuleType("qgis.core")
    core.QgsVectorLayer = FakeVectorLayer  # type: ignore[attr-defined]
    core.QgsFeatureRequest = FakeFeatureRequest  # type: ignore[attr-defined]
    core.NULL = NULL  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "qgis", types.ModuleType("qgis"))
    monkeypatch.setitem(sys.modules, "qgis.core", core)
    sys.modules["qgis"].core = core  # type: ignore[attr-defined]
    return core


# --- 辅助 / helpers ------------------------------------------------------------


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
    """像 QGIS 那样把后台线程与它的收尾跑完。

    快照准备就失败的调用根本没拿到任务，因此没有可跑的东西：它的记录已经说清了原因。

    Run the worker and its completion, the way QGIS would. A call whose snapshot
    preparation failed never got a task, so there is nothing to run: its record already
    says why.
    """

    task = harness.tasks[-1] if harness.tasks else None
    if task is not None and task.execution_id == submitted.execution_id:
        harness.run(task)
    return harness.record(submitted)


# --- 调用者看到什么 / what the caller sees -------------------------------------


def test_a_field_sum_is_computed_from_a_copied_snapshot() -> None:
    harness = harness_with(make_layer([(10,), (None,), (5,)]))
    submitted = submit_sum(harness)

    # submit 只接收并排队，快照下一回合才建 / submit only queues; the snapshot comes a turn later
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
        "selected_only": False,
        "snapshot_project_epoch": 0,
    }
    assert record.progress == 100


def test_a_selection_narrows_the_sum_and_says_so() -> None:
    """有选择集时只算选中的：桌面版统计面板的“仅选中要素”开关就是这件事。

    With a selection, only the selected features are summed: that is what the statistics
    panel's "selected features only" switch does (qgsstatisticalsummarydockwidget.cpp:210,
    :218 -> qgsvectorlayerutils.cpp:73-80).
    """

    harness = harness_with(make_layer([(1.0,), (2.0,), (3.0,)], selected_rows=[(2.0,), (3.0,)]))

    record = settled(harness, submit_sum(harness))

    assert record.status is SUCCEEDED
    assert record.result is not None
    assert record.result["count"] == 2
    assert record.result["total"] == 5.0
    assert record.result["selected_only"] is True
    # 选择集也走同一个裁剪请求 / the selection read takes the same trimmed request.
    assert harness.project.layers["layer-1"].last_request.attribute_indexes == [0]


def test_a_null_that_arrives_as_the_qgis_null_value_is_skipped() -> None:
    harness = harness_with(make_layer([(7,), (NULL,), (1,)]))

    record = settled(harness, submit_sum(harness))

    # 空值计入跳过的数，不当 0 求进和 / a null is counted as skipped, not summed as zero
    assert record.status is SUCCEEDED
    assert record.result is not None
    assert record.result["count"] == 2
    assert record.result["skipped_null"] == 1
    assert record.result["total"] == 8.0


def test_the_snapshot_asks_the_layer_for_only_the_field_it_sums() -> None:
    layer = make_layer([(1,)])
    harness = harness_with(layer)

    settled(harness, submit_sum(harness))

    # 只读一个字段、不要几何：请求必须带 NoGeometry 且裁剪到那一列
    # one field and no geometry: the request must carry NoGeometry and that column
    request = layer.last_request
    assert request is not None
    assert request.flags == FakeFeatureRequest.NoGeometry
    assert request.attribute_indexes == [0]


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

    # 任务声明它依赖哪个图层，QGIS 才能在图层消失时取消它
    # the task declares the layer it reads, which is what lets QGIS cancel it
    assert harness.tasks[0].dependent_layers == [layer]
    assert LayerSumNumericField().dependent_layer_ids(
        LayerSumNumericFieldParams(layer_id="layer-1", field_name=FIELD)
    ) == ["layer-1"]


# --- 还没有任务时就发生的拒绝 / refusals before any task exists ------------------


def test_a_synchronous_field_sum_is_unsupported_and_leaves_no_record() -> None:
    harness = harness_with(make_layer([(1,)]))

    with pytest.raises(FrameworkError) as caught:
        harness.engine.run_sync(SUM, {"layer_id": "layer-1", "field_name": FIELD})

    # 后台操作不能同步跑，被拒的调用也没变成一次执行
    # a background operation cannot be run synchronously, and the refused call never became an
    # execution
    assert caught.value.code == UNSUPPORTED_EXECUTION_MODE
    assert harness.engine._store.ids(set(ExecutionStatus)) == []
    assert harness.tasks == []


@pytest.mark.parametrize("value", [None, 1, "", True])
def test_the_parameters_accept_only_a_plain_layer_and_field_name(value: Any) -> None:
    with pytest.raises(ValidationError):
        LayerSumNumericFieldParams.model_validate({"layer_id": value, "field_name": FIELD})
    with pytest.raises(ValidationError):
        LayerSumNumericFieldParams.model_validate({"layer_id": "a", "field_name": value})


def test_an_undeclared_parameter_is_refused() -> None:
    with pytest.raises(ValidationError):
        LayerSumNumericFieldParams.model_validate(
            {"layer_id": "a", "field_name": FIELD, "limit": 10}
        )


def test_the_operation_registers_as_a_background_query() -> None:
    registry = OperationRegistry()
    registry.register(LayerSumNumericField())

    spec = registry.get_spec(SUM)

    assert spec.kind is OperationKind.QUERY
    assert spec.destructive is False
    assert LayerSumNumericField.strategy.value == "background"


# --- 快照阶段的拒绝 / snapshot refusals -----------------------------------------


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


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_value_is_refused_rather_than_put_in_the_record(value: float) -> None:
    # DOUBLE 能存 NaN/Infinity，QGIS 原样交回，于是和是 NaN，结果模型照收，
    # 而记录不再是可解析的 JSON（实测 ``allow_nan=False`` 会抛）
    # a DOUBLE can hold NaN and Infinity, which QGIS hands back as a float: the sum is NaN, the
    # model accepts it, and the record is no longer JSON an RFC 8259 parser reads (measured:
    # ``allow_nan=False`` raises)
    harness = harness_with(make_layer([(1.0,), (value,), (2.0,)]))

    record = settled(harness, submit_sum(harness))

    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == INVALID_FIELD_TYPE
    assert "non-finite" in record.error.message
    assert record.result is None
    assert harness.tasks == []


def test_a_sum_of_finite_values_that_overflows_is_refused() -> None:
    # 每个值都有限，1e308+1e308 却溢出成 Infinity，所以总和本身也要查是否有限
    # every value is finite, yet 1e308 + 1e308 overflows to Infinity, so the total itself has
    # to be checked for finiteness; total=inf would not be JSON an RFC 8259 parser reads
    harness = harness_with(make_layer([(1e308,), (1e308,), (5.0,)]))

    record = settled(harness, submit_sum(harness))

    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == INVALID_FIELD_TYPE
    assert "not a finite number" in record.error.message
    assert record.result is None
    assert harness.tasks != []


def test_a_sum_of_finite_values_is_still_written_and_parsed_strictly() -> None:
    # 上一条的姊妹例：拒绝不该连正常求和也一起没收
    # the sibling of that case: a refusal may not cost a normal sum its record
    harness = harness_with(make_layer([(1.5,), (2.5,)]))

    record = settled(harness, submit_sum(harness))

    assert record.status is SUCCEEDED
    assert record.result is not None and record.result["total"] == 4.0
    assert json.loads(json.dumps(record.result, allow_nan=False))["total"] == 4.0


# --- 取消 / cancellation ------------------------------------------------------


def test_cancelling_a_scheduled_task_waits_for_qgis_to_confirm() -> None:
    harness = harness_with(make_layer([(1,)]))
    submitted = submit_sum(harness)

    receipt = harness.engine.cancel(submitted.execution_id)

    # 请求被记下、任务被叫停，但还没有任何一方声称它已终止
    # the request is recorded and the task is asked to stop, but nothing claims it has
    # terminated yet
    assert receipt.accepted is True
    assert receipt.status is CANCEL_REQUESTED
    assert harness.tasks[0].isCanceled() is True
    assert harness.record(submitted).status is CANCEL_REQUESTED

    harness.tasks[0].finished(False)  # QGIS 确认终止 / QGIS confirms the termination

    assert harness.record(submitted).status is CANCELLED
    assert harness.record(submitted).result is None


def test_a_worker_that_finishes_after_a_cancel_reports_cancelled_without_a_result() -> None:
    harness = harness_with(make_layer([(4,)]))
    submitted = submit_sum(harness)
    harness.engine.cancel(submitted.execution_id)

    # 请求之后 ``run`` 仍可能返回 True，此时结果直接丢弃
    # ``run`` may still return True after the request and the result is dropped
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
    # CANCEL_REQUESTED 仍可到 FAILED，诊断保留下来
    # CANCEL_REQUESTED still goes on to FAILED and the diagnostic is kept
    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == INTERNAL_ERROR


def test_a_cancel_while_finalizing_is_refused() -> None:
    harness = harness_with(make_layer([(1,)]))
    submitted = submit_sum(harness)

    # 借存储把记录推进到 FINALIZING，适配器自己进这一步只持续一次调用，外部够不到它
    # walk the record into FINALIZING through the store: the adapter's own step into that state
    # lasts one call, so it is not reachable from outside
    store = harness.engine._store
    store.transition(submitted.execution_id, ExecutionStatus.RUNNING)
    store.transition(submitted.execution_id, FINALIZING)
    receipt = harness.engine.cancel(submitted.execution_id)

    assert receipt.accepted is False
    assert receipt.reason == "FINALIZING"


# --- 工程代数 / the project generation -----------------------------------------


def test_a_result_from_a_replaced_project_is_stale_project() -> None:
    harness = harness_with(make_layer([(1,)]))
    submitted = submit_sum(harness)

    harness.lifecycle.bump()  # aboutToBeCleared 上的代数递增 / what the bump does there

    record = settled(harness, submitted)
    # 旧代数的数字绝不进新工程 / the old generation's numbers never reach the new project
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

    # 引擎仍开着：只丢了旧代数 / the engine stays open: only the old generation was dropped
    assert harness.record(submitted).status is SCHEDULED
    assert harness.tasks[0].isCanceled() is False


# --- 关停 / shutdown ----------------------------------------------------------


def test_shutdown_stops_what_this_plugin_started_and_refuses_new_work() -> None:
    harness = harness_with(make_layer([(1,)]))
    first = submit_sum(harness)
    second = submit_sum(harness)
    waiting = submit_sum(harness)

    harness.engine.shutdown()

    # 排队的全部取消，每个活着的任务都被叫停
    # everything queued is cancelled and every live task is asked to stop
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


# --- 收尾阶段 / the finalize stage ---------------------------------------------


class BadFinalize(Counting):
    """后台线程成功、但收尾会炸的操作。

    A worker that succeeds and a finalize that breaks.
    """

    name = "test.bad_finalize"
    description = "raise while the result is being applied"

    def finalize_main(self, ctx: OperationContext, request: Params, result: int) -> DoneResult:
        raise RuntimeError("finalize broke")


class LyingFinalize(Counting):
    """返回的不是它声明的输出模型的那种收尾。

    A finalize that returns something other than its declared output model.
    """

    name = "test.lying_finalize"
    description = "return an object instead of its output model"

    def finalize_main(self, ctx: OperationContext, request: Params, result: int) -> DoneResult:
        return object()  # type: ignore[return-value]


def test_a_finalize_that_raises_is_a_failed_record() -> None:
    harness = Harness(BadFinalize())
    submitted = harness.submit(BadFinalize.name, {"amount": 1})

    harness.run(harness.tasks[0])

    record = harness.record(submitted)
    # 坏掉的收尾绝不能被当成成功 / a broken finalize may never look like a success
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


# --- 后台线程自己的契约 / the worker's own contract -----------------------------


def test_the_worker_gets_a_tuple_and_a_control_and_nothing_else() -> None:
    harness = harness_with(make_layer([(3.5,), (1.5,)]))

    submit_sum(harness)
    snapshot = harness.tasks[0].snapshot

    assert snapshot.values == (3.5, 1.5)
    assert not hasattr(snapshot, "mapLayer")
    result = LayerSumNumericField().execute_worker(snapshot, _NoCancel())
    # 后台线程声明的结果，主线程还没把它变成输出
    # the declared worker result, before the main thread turns it into the output
    assert isinstance(result, FieldSum)
    assert (result.count, result.total, result.skipped_null) == (2, 5.0, 0)


class _NoCancel:
    """从不取消、只记下收到多少进度的 TaskControl。

    A TaskControl that never cancels and records the progress it was given.
    """

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
        selected_only=False,
        project_epoch=0,
    )

    result = LayerSumNumericField().execute_worker(snapshot, Cancelling())

    # 残缺的数字原样交回，由适配器丢弃
    # the partial numbers are handed back untouched, and the adapter drops them
    assert result.count == 9
    assert result.total == sum(range(9))


def test_the_worker_reports_progress_across_the_whole_range() -> None:
    snapshot = FieldValues(
        layer_id="layer-1",
        field_name=FIELD,
        values=tuple(1.0 for _ in range(400)),
        skipped_null=0,
        selected_only=False,
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
    request = LayerSumNumericFieldParams(layer_id="layer-1", field_name=FIELD)
    snapshot = FieldValues(
        layer_id="layer-1",
        field_name=FIELD,
        values=(1.0,),
        skipped_null=0,
        selected_only=False,
        project_epoch=0,
    )
    result = operation.execute_worker(snapshot, _NoCancel())
    harness.lifecycle.bump()

    with pytest.raises(FrameworkError) as caught:
        operation.finalize_main(harness.engine._context(), request, result)

    # 操作自己也查代数，不只靠适配器 / the operation checks the epoch itself, not only the adapter
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

    # 进度回报才把 SCHEDULED 推到 RUNNING，这一步在主线程上
    # a progress report is what moves SCHEDULED to RUNNING on the main thread
    harness.manager.report(harness.tasks[0], 50.0)

    assert harness.record(submitted).status is RUNNING
    assert harness.record(submitted).progress == 50.0
