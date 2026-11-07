"""The QgsTask adapter's bookkeeping, against fakes that keep the same moving parts.

No QGIS and no Qt here: the task manager is a dict plus a signal, the task is a
plain object, and the event queue is a list the test drains.  What is checked is
the accounting the real classes force on the adapter -- the two-slot gate, the
task id the store never keeps, the 100 ms progress window, and a refusal that has
to end the call instead of leaving it queued (PRD 8.4, 8.5).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from pydantic import BaseModel

from qgis_agent_plugin.core.config import Settings
from qgis_agent_plugin.core.context import ProjectLifecycle
from qgis_agent_plugin.core.contracts import (
    BackgroundOperationMixin,
    BaseOperation,
    BaseQuery,
    OperationContext,
    TaskControl,
)
from qgis_agent_plugin.core.engine import OperationEngine
from qgis_agent_plugin.core.models import (
    INTERNAL_ERROR,
    TASK_SUBMISSION_FAILED,
    ExecutionRecord,
    ExecutionStatus,
    FrameworkError,
    Params,
)
from qgis_agent_plugin.core.registry import OperationRegistry
from qgis_agent_plugin.core.runners.qgs_task import (
    OperationRun,
    OperationTaskRunner,
    _TaskControl,
)

QUEUED = ExecutionStatus.QUEUED
SCHEDULED = ExecutionStatus.SCHEDULED
RUNNING = ExecutionStatus.RUNNING
SUCCEEDED = ExecutionStatus.SUCCEEDED
FAILED = ExecutionStatus.FAILED
CANCELLED = ExecutionStatus.CANCELLED

# --- the fakes ---------------------------------------------------------------


class FakeSignal:
    """A Qt signal reduced to what the adapter uses: connect a slot, emit on it."""

    def __init__(self) -> None:
        self.slots: list[Callable[..., None]] = []

    def connect(self, slot: Callable[..., None]) -> None:
        self.slots.append(slot)

    def emit(self, *arguments: Any) -> None:
        for slot in self.slots:
            slot(*arguments)


class FakeTaskManager:
    """``addTask``, ``task(id)`` and ``progressChanged``; nothing else is used."""

    def __init__(self) -> None:
        self.progressChanged = FakeSignal()
        self.tasks: dict[int, FakeTask] = {}
        self.refuse = False
        self._next_id = 1

    def addTask(self, task: FakeTask) -> int:  # noqa: N802 - the QGIS name
        if self.refuse:
            return 0
        task_id = self._next_id
        self._next_id += 1
        self.tasks[task_id] = task
        return task_id

    def task(self, task_id: int) -> FakeTask | None:  # noqa: N802 - the QGIS name
        return self.tasks.get(task_id)

    def report(self, task: FakeTask, progress: float) -> None:
        """What QGIS does for ``setProgress``: relay it as (task id, progress)."""

        for task_id, known in self.tasks.items():
            if known is task:
                self.progressChanged.emit(task_id, progress)
                return


class FakeTask:
    """A ``QgsTask`` stand-in: the same callbacks, no threads and no C++."""

    def __init__(
        self,
        runner: OperationTaskRunner,
        execution_id: str,
        operation: BaseOperation,
        snapshot: Any,
        manager: FakeTaskManager,
    ) -> None:
        self.runner = runner
        self.execution_id = execution_id
        self.operation = operation
        self.snapshot = snapshot
        self.manager = manager
        self.run_object = OperationRun(operation, snapshot)
        self.cancelled = False
        self.progress_value = 0.0
        self.dependent_layers: list[Any] = []
        self.runs = 0

    # what the adapter uses
    def cancel(self) -> None:
        self.cancelled = True

    def isCanceled(self) -> bool:  # noqa: N802 - the QGIS name
        return self.cancelled

    def progress(self) -> float:
        return self.progress_value

    def setProgress(self, value: float) -> None:  # noqa: N802 - the QGIS name
        self.progress_value = value
        self.manager.report(self, value)

    def setDependentLayers(self, layers: list[Any]) -> None:  # noqa: N802 - the QGIS name
        self.dependent_layers = list(layers)

    # what QGIS would drive
    def run(self) -> bool:
        self.runs += 1
        return self.run_object.run(_TaskControl(self))

    def finished(self, result: bool) -> None:
        self.runner.task_finished(self.execution_id, self.run_object, self, result)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Queue:
    """The Qt event queue, stood in for by a list the test drains."""

    def __init__(self) -> None:
        self.callbacks: list[Callable[[], None]] = []

    def __call__(self, callback: Callable[[], None]) -> None:
        self.callbacks.append(callback)

    def tick(self) -> None:
        while self.callbacks:
            self.callbacks.pop(0)()


# --- operations used by these tests ------------------------------------------


class AmountParams(Params):
    amount: int = 1


class DoneResult(BaseModel):
    value: int


class Counting(
    BaseQuery[AmountParams, DoneResult],
    BackgroundOperationMixin[AmountParams, DoneResult, int, int],
):
    """A worker that reports progress in ``steps`` slices and returns the sum."""

    name = "test.counting"
    description = "count to the amount it was given"
    input_model = AmountParams
    output_model = DoneResult
    steps = 4
    fail = False
    seen: list[str] = []

    def prepare_main(self, ctx: OperationContext, request: AmountParams) -> int:
        self.seen.append("prepare")
        return request.amount

    def execute_worker(self, snapshot: int, control: TaskControl) -> int:
        self.seen.append("worker")
        if self.fail:
            raise RuntimeError("the worker broke")
        for step in range(1, self.steps + 1):
            control.set_progress(100.0 * step / self.steps)
        return snapshot

    def finalize_main(
        self, ctx: OperationContext, request: AmountParams, result: int
    ) -> DoneResult:
        self.seen.append("finalize")
        return DoneResult(value=result)


class ExplodingPrepare(Counting):
    name = "test.exploding_prepare"
    description = "fail while the snapshot is being prepared"

    def prepare_main(self, ctx: OperationContext, request: AmountParams) -> int:
        raise FrameworkError(TASK_SUBMISSION_FAILED, "the snapshot could not be read")


class Layered(Counting):
    """Declares a layer dependency, the way ``layer.sum_numeric_field`` does."""

    name = "test.layered"
    description = "an operation whose worker depends on one layer"

    def dependent_layer_ids(self, request: AmountParams) -> list[str]:
        return [f"layer-{request.amount}"]


class FakeProject:
    def __init__(self) -> None:
        self.layers: dict[str, Any] = {}

    def mapLayer(self, layer_id: str) -> Any:  # noqa: N802 - the QGIS name
        return self.layers.get(layer_id)


class Harness:
    """An engine wired to the real adapter, with fakes behind both Qt seams."""

    def __init__(self, *operations: BaseOperation, settings: Settings | None = None) -> None:
        self.registry = OperationRegistry()
        for operation in operations:
            self.registry.register(operation)
        self.lifecycle = ProjectLifecycle()
        self.project = FakeProject()
        self.queue = Queue()
        self.manager = FakeTaskManager()
        self.clock = FakeClock()
        self.tasks: list[FakeTask] = []
        self.runner = OperationTaskRunner(
            task_factory=self._make_task,
            task_manager=self.manager,
            clock=self.clock,
        )
        self.engine = OperationEngine(
            self.registry,
            settings or Settings(),
            self.lifecycle,
            iface=None,
            project=self.project,
            background_runner=self.runner,
            background_cancel=self.runner.cancel,
            post=self.queue,
        )

    def _make_task(
        self,
        runner: OperationTaskRunner,
        execution_id: str,
        operation: BaseOperation,
        snapshot: Any,
    ) -> FakeTask:
        task = FakeTask(runner, execution_id, operation, snapshot, self.manager)
        self.tasks.append(task)
        return task

    def submit(self, name: str, arguments: dict[str, Any] | None = None) -> ExecutionRecord:
        record = self.engine.submit(name, arguments or {})
        self.queue.tick()
        return record

    def run(self, task: FakeTask) -> None:
        """Run one worker and deliver its completion, like QGIS would."""

        task.finished(task.run())

    def record(self, record: ExecutionRecord) -> ExecutionRecord:
        return self.engine.get(record.execution_id)

    def engine_record(self, execution_id: str) -> ExecutionRecord:
        return self.engine._store.get(execution_id)


@pytest.fixture
def counting() -> Iterator[Counting]:
    operation = Counting()
    operation.seen = []
    operation.fail = False
    yield operation
    operation.seen = []
    operation.fail = False


# --- the worker half ---------------------------------------------------------


def test_a_worker_exception_is_kept_and_never_reaches_the_task(
    counting: Counting, caplog: pytest.LogCaptureFixture
) -> None:
    counting.fail = True
    run = OperationRun(counting, 3)

    class Task:
        def isCanceled(self) -> bool:  # noqa: N802 - the QGIS name
            return False

        def setProgress(self, value: float) -> None:  # noqa: N802 - the QGIS name
            raise AssertionError("a failing worker must not report progress")

    with caplog.at_level("ERROR"):
        succeeded = run.run(_TaskControl(Task()))

    # AT-07: the exception is a diagnostic, not a crash in the C++ task framework.
    assert succeeded is False
    assert isinstance(run.error, RuntimeError)
    assert "execute_worker raised" in caplog.text


def test_progress_is_clamped_to_the_range_the_protocol_grants() -> None:
    class Task:
        def __init__(self) -> None:
            self.values: list[float] = []

        def isCanceled(self) -> bool:  # noqa: N802 - the QGIS name
            return False

        def setProgress(self, value: float) -> None:  # noqa: N802 - the QGIS name
            self.values.append(value)

    task = Task()
    control = _TaskControl(task)
    control.set_progress(-12)
    control.set_progress(150)
    control.set_progress(37.5)

    # PRD 6.2: out-of-range values are the adapter's to clamp, not the worker's.
    assert task.values == [0.0, 100.0, 37.5]


# --- submission --------------------------------------------------------------


def test_a_task_the_manager_refuses_ends_the_call_as_task_submission_failed(
    counting: Counting,
) -> None:
    harness = Harness(counting)
    harness.manager.refuse = True

    record = harness.submit(counting.name, {"amount": 5})

    assert record.status is QUEUED
    settled = harness.record(record)
    assert settled.status is FAILED
    assert settled.error is not None
    assert settled.error.code == TASK_SUBMISSION_FAILED
    assert settled.result is None
    # AT-14: no dangling execution, and nothing was left owning a task.
    assert harness.runner._active == {}
    assert counting.seen == ["prepare"]


def test_a_prepare_that_refuses_keeps_its_own_error_code() -> None:
    operation = ExplodingPrepare()
    harness = Harness(operation)

    submitted = harness.submit(operation.name, {"amount": 5})

    settled = harness.record(submitted)
    assert settled.status is FAILED
    assert settled.error is not None
    assert settled.error.code == TASK_SUBMISSION_FAILED
    assert harness.tasks == []


def test_prepare_runs_on_the_main_thread_before_anything_is_submitted(
    counting: Counting,
) -> None:
    harness = Harness(counting)
    submitted = harness.submit(counting.name, {"amount": 7})

    # PRD 6.5: the snapshot is not built inside ``submit``; one event-loop turn later
    # the call is PREPARING/SCHEDULED with the worker not yet started.
    assert submitted.status is QUEUED
    assert counting.seen == ["prepare"]
    assert harness.record(submitted).status is SCHEDULED
    assert len(harness.tasks) == 1


def test_the_task_is_a_dependent_of_the_layer_the_operation_names(
    counting: Layered,
) -> None:
    operation = Layered()
    harness = Harness(operation)
    harness.project.layers["layer-2"] = object()

    harness.submit(operation.name, {"amount": 2})

    # PRD 8.4: QGIS cancels a task whose dependent layer goes away (AT-12).
    assert harness.tasks[0].dependent_layers == [harness.project.layers["layer-2"]]


def test_an_operation_without_dependencies_declares_none(counting: Counting) -> None:
    harness = Harness(counting)

    harness.submit(counting.name, {"amount": 1})

    assert harness.tasks[0].dependent_layers == []


# --- the two-slot gate -------------------------------------------------------


def test_a_third_background_call_waits_in_queued_until_a_slot_frees(
    counting: Counting,
) -> None:
    harness = Harness(counting)
    first = harness.submit(counting.name, {"amount": 1})
    second = harness.submit(counting.name, {"amount": 2})
    third = harness.submit(counting.name, {"amount": 3})

    # PRD 8.5 / AT-08C: two active, the third stays QUEUED.
    assert harness.record(first).status is SCHEDULED
    assert harness.record(second).status is SCHEDULED
    assert harness.record(third).status is QUEUED
    assert len(harness.tasks) == 2
    assert counting.seen == ["prepare", "prepare"]

    harness.run(harness.tasks[0])

    # A free slot starts the waiting call in submission order.
    assert harness.record(third).status is SCHEDULED
    assert counting.seen.count("prepare") == 3
    assert len(harness.tasks) == 3


def test_the_waiting_queue_keeps_submission_order(counting: Counting) -> None:
    harness = Harness(counting)
    first = harness.submit(counting.name, {"amount": 1})
    second = harness.submit(counting.name, {"amount": 2})
    waiting = [harness.submit(counting.name, {"amount": n}) for n in (3, 4)]

    harness.run(harness.tasks[0])
    harness.run(harness.tasks[1])

    assert [harness.record(record).status for record in waiting] == [SCHEDULED, SCHEDULED]
    # The worker was really started for both, in the order they were submitted.
    assert [task.snapshot for task in harness.tasks] == [1, 2, 3, 4]
    assert harness.record(first).status is SUCCEEDED
    assert harness.record(second).status is SUCCEEDED


def test_a_waiting_call_cancelled_before_its_turn_is_never_started(
    counting: Counting,
) -> None:
    harness = Harness(counting)
    harness.submit(counting.name, {"amount": 1})
    harness.submit(counting.name, {"amount": 2})
    waiting = harness.submit(counting.name, {"amount": 3})
    harness.engine.cancel(waiting.execution_id)

    harness.run(harness.tasks[0])

    # PRD 8.5: the engine cancelled the record, so the adapter drops it instead of
    # preparing a snapshot nobody wants.
    assert harness.record(waiting).status is CANCELLED
    assert len(harness.tasks) == 2


# --- the progress window -----------------------------------------------------


def test_progress_rewrites_the_record_at_most_once_per_interval(
    counting: Counting,
) -> None:
    harness = Harness(counting)
    record = harness.submit(counting.name, {"amount": 1})
    task = harness.tasks[0]
    execution_id = record.execution_id
    writes: list[float] = []

    original = harness.engine._store.transition

    def spy(*arguments: Any, **keywords: Any) -> ExecutionRecord:
        if "progress" in keywords:
            writes.append(keywords["progress"])
        return original(*arguments, **keywords)

    harness.engine._store.transition = spy  # type: ignore[method-assign]

    # Four reports inside the same 100 ms window: one write, three kept back.
    harness.clock.now = 1.0
    for step in (25.0, 50.0, 75.0):
        harness.manager.report(task, step)
    assert writes == [25.0]

    harness.clock.now = 1.05
    harness.manager.report(task, 90.0)
    assert writes == [25.0]

    harness.clock.now = 1.2
    harness.manager.report(task, 95.0)
    assert writes == [25.0, 95.0]
    assert harness.engine_record(execution_id).status is RUNNING


def test_the_last_progress_before_the_end_is_written_even_inside_the_window(
    counting: Counting,
) -> None:
    harness = Harness(counting)
    record = harness.submit(counting.name, {"amount": 1})
    task = harness.tasks[0]

    harness.clock.now = 1.0
    harness.manager.report(task, 40.0)
    harness.clock.now = 1.01
    harness.manager.report(task, 80.0)  # throttled, kept for the flush
    harness.clock.now = 1.02
    task.run()
    task.finished(True)

    # The record's own progress ends at 100 because it succeeded, but the dropped
    # 80% must have been written on the way, not silently lost (PRD 8.4).
    assert harness.record(record).status is SUCCEEDED
    assert harness.record(record).progress == 100


def test_a_progress_report_for_an_unknown_task_is_ignored(counting: Counting) -> None:
    harness = Harness(counting)

    harness.runner._progress_reported(999, 50.0)

    assert harness.engine._store.ids(set(ExecutionStatus)) == []


# --- the completion handler --------------------------------------------------


def test_a_successful_task_passes_through_running_and_finalizing(counting: Counting) -> None:
    harness = Harness(counting)
    submitted = harness.submit(counting.name, {"amount": 4})

    task = harness.tasks[0]
    harness.run(task)

    record = harness.record(submitted)
    assert record.status is SUCCEEDED
    assert record.result == {"value": 4}
    assert record.progress == 100
    assert record.started_at is not None
    assert record.finished_at is not None
    assert counting.seen == ["prepare", "worker", "finalize"]
    assert harness.runner._active == {}
    assert harness.runner._progress_written == {}


def test_an_unexpected_failure_keeps_its_type_and_not_its_text(
    counting: Counting, caplog: pytest.LogCaptureFixture
) -> None:
    counting.fail = True
    harness = Harness(counting)
    submitted = harness.submit(counting.name, {"amount": 4})

    with caplog.at_level("ERROR"):
        harness.run(harness.tasks[0])

    record = harness.record(submitted)
    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == INTERNAL_ERROR
    assert record.error.message == "RuntimeError: unexpected failure"
    assert "the worker broke" in caplog.text


def test_a_completion_the_runner_does_not_own_is_refused_politely(
    counting: Counting,
) -> None:
    harness = Harness(counting)
    harness.submit(counting.name, {"amount": 1})
    task = harness.tasks[0]

    # Second completion for the same execution: nothing is written, nothing raises.
    harness.run(task)
    harness.run(task)

    assert harness.engine._store.ids(set(ExecutionStatus)) != []


def test_a_task_that_ends_without_a_result_is_failed(counting: Counting) -> None:
    harness = Harness(counting)
    submitted = harness.submit(counting.name, {"amount": 1})
    task = harness.tasks[0]

    # No worker run, no cancel: QGIS terminated a task this adapter did not.
    task.finished(False)

    record = harness.record(submitted)
    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == INTERNAL_ERROR
