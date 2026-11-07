"""Background operations as ``QgsTask``s: two at a time, cancellable, never raising.

The engine hands an accepted background call to this adapter one event-loop turn
after ``submit`` (PRD 6.5).  From there the adapter owns the parts the engine may
not touch: ``prepare_main`` on the main thread, the ``QgsTaskManager`` submission,
the worker thread, and the completion callback back on the main thread.

Three rules shape the code.  A status is written from the main thread only, so a
worker reports progress through ``QgsTask.setProgress`` and the adapter reads it
back from the manager's ``progressChanged`` signal, which Qt delivers on the main
thread; the store is written at most once per 100 ms, except for the last value
before the task ends.  Nothing may raise into a Qt slot or into the C++ task
framework, so the worker's exception is captured as a diagnostic and both callbacks
of the task shell are guarded.  And a ``QgsTask`` is never kept: QGIS deletes its
C++ side as soon as the task ends, so only its task id is held while it lives and
nothing of it survives into the record.

QGIS is imported inside ``_build_task_class`` and ``_task_manager``, which keeps
this module importable in a bare interpreter like the rest of the core.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel

from ..contracts import BackgroundOperationMixin, BaseOperation, TaskControl
from ..engine import _serialised_output
from ..models import (
    INTERNAL_ERROR,
    STALE_PROJECT,
    TASK_SUBMISSION_FAILED,
    ErrorDetail,
    ExecutionStatus,
    FrameworkError,
    utc_now,
)

if TYPE_CHECKING:
    from ..engine import OperationEngine
    from ..execution_store import ExecutionStore

_LOGGER = logging.getLogger(__name__)

# How often a running task may rewrite its execution record (PRD 8.4).  The value
# reported last before the task ends is written regardless: a throttled drop there
# would leave a finished record short of what the worker actually reached.
PROGRESS_INTERVAL_SECONDS = 0.1

_TERMINAL = frozenset(
    {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}
)

_UNSET = object()


class OperationRun:
    """What one worker produced, kept apart from the ``QgsTask`` that carried it.

    ``run`` never raises: an exception escaping into the C++ task framework aborts
    QGIS instead of failing one execution, so it is kept as a diagnostic and the
    task reports failure (PRD 8.4, 11.3, AT-07).
    """

    def __init__(self, operation: BaseOperation, snapshot: Any) -> None:
        self._operation = operation
        self._snapshot = snapshot
        self.result: Any = _UNSET
        self.error: BaseException | None = None

    def run(self, control: TaskControl) -> bool:
        try:
            self.result = cast(BackgroundOperationMixin, self._operation).execute_worker(
                self._snapshot, control
            )
        except BaseException as error:  # noqa: B036 - nothing may reach the C++ task layer
            self.error = error
            _LOGGER.exception("%s: execute_worker raised", self._operation.name)
            return False
        return True


class _TaskControl:
    """The worker's whole view of its execution: a cancel flag and progress."""

    def __init__(self, task: Any) -> None:
        self._task = task

    def is_cancelled(self) -> bool:
        return bool(self._task.isCanceled())

    def set_progress(self, value: float) -> None:
        # The protocol grants 0-100; clamping is the adapter's job (PRD 6.2).
        self._task.setProgress(max(0.0, min(100.0, float(value))))


@dataclass
class _Submission:
    """One accepted background call: what it needs to run, and its task once it has one."""

    engine: OperationEngine
    execution_id: str
    operation: BaseOperation
    request: BaseModel
    task_id: int = 0


class OperationTaskRunner:
    """Submit background operations to a ``QgsTaskManager``, two at a time (PRD 8.4, 8.5)."""

    def __init__(
        self,
        *,
        task_factory: Callable[..., Any] | None = None,
        task_manager: Any = None,
        clock: Callable[[], float] | None = None,
        progress_interval: float = PROGRESS_INTERVAL_SECONDS,
    ) -> None:
        self._new_task = task_factory or task_class()
        self._progress_interval = progress_interval
        self._clock = clock or time.monotonic
        self._manager = task_manager
        # Active tasks by execution id, and the calls still waiting for a slot in
        # submission order.  Both are emptied as work finishes, so neither is history.
        self._active: dict[str, _Submission] = {}
        self._waiting: deque[_Submission] = deque()
        self._progress_written: dict[str, float] = {}
        self._progress_pending: dict[str, float] = {}
        if task_manager is not None:
            task_manager.progressChanged.connect(self._progress_reported)

    def __call__(
        self,
        engine: OperationEngine,
        record: Any,
        operation: BaseOperation,
        request: BaseModel,
    ) -> None:
        """The engine's seam: one accepted background call, still ``QUEUED``."""

        submission = _Submission(engine, record.execution_id, operation, request)
        if len(self._active) >= engine._settings.max_active_background:
            # The call keeps its QUEUED record until a slot frees (PRD 8.5, AT-08C).
            self._waiting.append(submission)
            return
        self._start(submission)

    def cancel(self, engine: OperationEngine, execution_id: str) -> None:
        """Ask one task to stop; the engine has already written the request (PRD 8.5).

        Never ``QgsTaskManager.cancelAll()``: that is the whole QGIS session's queue,
        not this plugin's own tasks.
        """

        submission = self._active.get(execution_id)
        manager = self._manager
        if submission is None or manager is None:
            return
        task = manager.task(submission.task_id)
        if task is None:
            return
        try:
            task.cancel()
        except Exception:  # noqa: BLE001 - a task QGIS already dropped may not raise here
            _LOGGER.exception("cancel of task %s failed", submission.task_id)

    def task_finished(
        self, execution_id: str, run: OperationRun, task: Any, succeeded: bool
    ) -> None:
        """The main-thread completion handler for one task (PRD 8.4).

        ``task`` is alive for the length of this call and is never kept: QGIS
        deletes its C++ side as soon as the task ends.
        """

        submission = self._active.pop(execution_id, None)
        pending = self._progress_pending.pop(execution_id, None)
        self._progress_written.pop(execution_id, None)
        if submission is None:
            _LOGGER.warning(
                "completion for an execution this runner does not own: %s", execution_id
            )
            return
        store = submission.engine._store
        record = store.get(execution_id)
        if pending is not None and record.status not in _TERMINAL:
            record = store.transition(execution_id, progress=pending)

        if run.error is not None:
            # A real failure outranks a cancel request (PRD 8.2: CANCEL_REQUESTED -> FAILED).
            self._running(store, execution_id, record)
            self._fail(store, execution_id, INTERNAL_ERROR, _unexpected(run.error))
        elif bool(task.isCanceled()) or record.status is ExecutionStatus.CANCEL_REQUESTED:
            if record.status is ExecutionStatus.SCHEDULED:
                # Cancelled while QGIS still held it: the request is reported first,
                # because SCHEDULED has no edge straight to CANCELLED (PRD 8.2, AT-08B).
                store.transition(execution_id, ExecutionStatus.CANCEL_REQUESTED)
            store.transition(execution_id, ExecutionStatus.CANCELLED)
        elif not succeeded:
            self._running(store, execution_id, record)
            self._fail(store, execution_id, INTERNAL_ERROR, "the task ended without a result")
        else:
            self._finish_results(
                submission, self._running(store, execution_id, record), run.result
            )
        self._drain()

    def _start(self, submission: _Submission) -> None:
        """Take one QUEUED call to SCHEDULED, or to the record that says why not."""

        engine = submission.engine
        execution_id = submission.execution_id
        operation = submission.operation
        store = engine._store
        if store.get(execution_id).project_epoch != engine._lifecycle.current_epoch:
            # Accepted under the old project: not even a snapshot may be read now.
            store.transition(execution_id, ExecutionStatus.CANCELLED)
            return
        store.transition(execution_id, ExecutionStatus.PREPARING, started_at=utc_now())
        try:
            snapshot = cast(BackgroundOperationMixin, operation).prepare_main(
                engine._context(), submission.request
            )
        except FrameworkError as error:
            self._fail(store, execution_id, error.code, error.message)
            return
        except Exception as error:  # noqa: BLE001 - a prepare bug is a FAILED record
            _LOGGER.exception("%s: prepare_main raised", operation.name)
            self._fail(store, execution_id, INTERNAL_ERROR, _unexpected(error))
            return

        task = self._new_task(self, execution_id, operation, snapshot)
        dependent = _dependent_layers(engine, operation, submission.request)
        if dependent:
            task.setDependentLayers(dependent)
        try:
            task_id = self._task_manager().addTask(task)
        except Exception:  # noqa: BLE001 - a PyQGIS refusal is still a submission failure
            _LOGGER.exception("%s: addTask raised", operation.name)
            task_id = 0
        if not task_id:
            # The manager took no ownership, so the task object is ours to drop.
            self._fail(
                store,
                execution_id,
                TASK_SUBMISSION_FAILED,
                f"{operation.name}: the task manager refused the task",
            )
            return
        submission.task_id = task_id
        self._active[execution_id] = submission
        store.transition(execution_id, ExecutionStatus.SCHEDULED)

    def _drain(self) -> None:
        """Start waiting calls while a slot is free, in submission order (PRD 8.5)."""

        while self._waiting:
            head = self._waiting[0]
            if len(self._active) >= head.engine._settings.max_active_background:
                return
            self._waiting.popleft()
            try:
                if head.engine._store.get(head.execution_id).status is not ExecutionStatus.QUEUED:
                    continue
            except FrameworkError:
                continue
            self._start(head)

    def _finish_results(self, submission: _Submission, record: Any, output: Any) -> None:
        """Validate the project generation, finalise, and write the result (PRD 9.2)."""

        engine = submission.engine
        store = engine._store
        execution_id = submission.execution_id
        if record.project_epoch != engine._lifecycle.current_epoch:
            # An old generation's result never reaches the new project (PRD 9.1, AT-11).
            self._fail(
                store,
                execution_id,
                STALE_PROJECT,
                "the project was replaced while the task ran",
            )
            return
        store.transition(execution_id, ExecutionStatus.FINALIZING)
        try:
            result = cast(BackgroundOperationMixin, submission.operation).finalize_main(
                engine._context(), submission.request, output
            )
            serialised = _serialised_output(submission.operation, result)
        except FrameworkError as error:
            self._fail(store, execution_id, error.code, error.message)
            return
        except Exception as error:  # noqa: BLE001 - a finalize bug is a FAILED record
            _LOGGER.exception("%s: finalize_main raised", submission.operation.name)
            self._fail(store, execution_id, INTERNAL_ERROR, _unexpected(error))
            return
        store.transition(
            execution_id, ExecutionStatus.SUCCEEDED, result=serialised, progress=100
        )

    def _progress_reported(self, task_id: int, progress: float) -> None:
        """The manager's progress signal, already delivered on the main thread."""

        try:
            self._record_progress(task_id, progress)
        except Exception:  # noqa: BLE001 - a Qt slot may not raise
            _LOGGER.exception("progress for task %s failed", task_id)

    def _record_progress(self, task_id: int, progress: float) -> None:
        submission = self._submission_for(task_id)
        if submission is None:
            return
        execution_id = submission.execution_id
        now = self._clock()
        last = self._progress_written.get(execution_id)
        if last is not None and now - last < self._progress_interval:
            # One write per interval; the latest value is kept for the flush at the end.
            self._progress_pending[execution_id] = progress
            return
        store = submission.engine._store
        record = store.get(execution_id)
        if record.status in _TERMINAL:
            return
        if record.status is ExecutionStatus.SCHEDULED:
            # The first report is what tells the main thread the task really started
            # (PRD 8.2: SCHEDULED -> RUNNING), since ``run`` is on another thread.
            record = store.transition(execution_id, ExecutionStatus.RUNNING)
        store.transition(execution_id, progress=progress)
        self._progress_written[execution_id] = now

    def _submission_for(self, task_id: int) -> _Submission | None:
        for submission in self._active.values():
            if submission.task_id == task_id:
                return submission
        return None

    def _task_manager(self) -> Any:
        if self._manager is None:
            from qgis.core import QgsTaskManager

            manager = QgsTaskManager()
            manager.progressChanged.connect(self._progress_reported)
            self._manager = manager
        return self._manager

    @staticmethod
    def _running(store: ExecutionStore, execution_id: str, record: Any) -> Any:
        """Move SCHEDULED to RUNNING before anything else may be written (PRD 8.2)."""

        if record.status is ExecutionStatus.SCHEDULED:
            return store.transition(execution_id, ExecutionStatus.RUNNING)
        return record

    @staticmethod
    def _fail(store: ExecutionStore, execution_id: str, code: str, message: str) -> None:
        store.transition(
            execution_id, ExecutionStatus.FAILED, error=ErrorDetail(code=code, message=message)
        )


def _unexpected(error: BaseException) -> str:
    """A caller-safe message: the type only, never the exception's own text.

    The engine writes the same string for its own paths (PRD 11.1, 11.3); it is
    repeated here rather than imported because it is a private name of another
    module, and the rule is one line.
    """

    return f"{type(error).__name__}: unexpected failure"


def _dependent_layers(
    engine: OperationEngine, operation: BaseOperation, request: BaseModel
) -> list[Any]:
    """The layers a task depends on, so QGIS cancels it when one is removed (PRD 8.4).

    An operation opts in by declaring ``dependent_layer_ids(request)``; the three
    background stages are the frozen protocol, so the dependency is an optional
    extra rather than a fourth stage.
    """

    ids = getattr(operation, "dependent_layer_ids", None)
    if ids is None:
        return []
    project = engine._context().project
    return [layer for layer in (project.mapLayer(i) for i in ids(request)) if layer is not None]


_TASK_CLASS: type | None = None


def task_class() -> type:
    """The ``QgsTask`` subclass, built on first use so this module imports without QGIS."""

    global _TASK_CLASS
    if _TASK_CLASS is None:
        _TASK_CLASS = _build_task_class()
    return _TASK_CLASS


def _build_task_class() -> type:
    from qgis.core import QgsTask

    class OperationTask(QgsTask):
        """One background operation as a task QGIS owns from submission on (PRD 8.4)."""

        def __init__(
            self,
            runner: OperationTaskRunner,
            execution_id: str,
            operation: BaseOperation,
            snapshot: Any,
        ) -> None:
            super().__init__(operation.description, QgsTask.CanCancel)
            self._runner = runner
            self._execution_id = execution_id
            self._run = OperationRun(operation, snapshot)

        def run(self) -> bool:
            return self._run.run(_TaskControl(self))

        def finished(self, result: bool) -> None:
            # Always the main thread, always inside a Qt slot: an exception here would
            # abort QGIS instead of failing one execution (PRD 8.4, 11.3).
            try:
                self._runner.task_finished(self._execution_id, self._run, self, bool(result))
            except Exception:  # noqa: BLE001 - a Qt slot may not raise
                _LOGGER.exception("%s: completion handler raised", self._execution_id)

    return OperationTask
