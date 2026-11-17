"""后台操作就是 ``QgsTask``：并发受限、可取消、绝不抛异常。

引擎在 ``submit`` 之后一轮事件循环，把放行的后台调用交给这个适配器。
从这里开始，适配器负责引擎不能碰的部分：主线程上的 ``prepare_main``、
``QgsTaskManager`` 的提交、后台线程，以及回到主线程的完成回调。

三条规则决定了这段代码的形状。状态只从主线程写，所以后台线程用
``QgsTask.setProgress`` 报进度，适配器再从管理器的 ``progressChanged`` 信号读回来，
Qt 会在主线程投递该信号；存储每 100 毫秒最多写一次，任务结束前的最后一个值例外。
任何东西都不许抛进 Qt 槽或 C++ 任务框架，所以后台线程的异常被收下来当诊断，
适配器的每个 Qt 入口都加了守卫，包括投出去的排空回调。还有，``QgsTask`` 从不被保留：
任务一结束 QGIS 就删掉它的 C++ 部分，所以只在它活着时记住任务 id，
它身上没有任何东西活进记录里。

一次完成释放出来的槽位在一个事件循环轮次之后才补上，走的是引擎用的同一个投递函数。
在另一个任务的 ``finished()`` 回调里提交任务，正是 3.44.13 有时接受却从不运行的情形：
任务一直停在 ``Queued``，``finished()`` 永远不来，这个槽位整个会话就丢了。
投出去的排空会重新读槽位数和每个等待中的记录，所以这期间到达的取消或工程换代仍然优先。

QGIS 在 ``_build_task_class`` 内部导入，这使本模块和 core 其余部分一样能在裸解释器里导入。

Background operations as ``QgsTask``s: two at a time, cancellable, never raising.

The engine hands an accepted background call to this adapter one event-loop turn
after ``submit``.  From there the adapter owns the parts the engine may not
touch: ``prepare_main`` on the main thread, the ``QgsTaskManager`` submission,
the worker thread, and the completion callback back on the main thread.

Three rules shape the code.  A status is written from the main thread only, so a
worker reports progress through ``QgsTask.setProgress`` and the adapter reads it
back from the manager's ``progressChanged`` signal, which Qt delivers on the main
thread; the store is written at most once per 100 ms, except for the last value
before the task ends.  Nothing may raise into a Qt slot or into the C++ task
framework, so the worker's exception is captured as a diagnostic and every Qt entry
point of the adapter is guarded, the posted drain included.  And a ``QgsTask`` is
never kept: QGIS deletes its C++ side as soon as the task ends, so only its task id
is held while it lives and nothing of it survives into the record.

A slot freed by a completion is filled one event-loop turn later, through the same
poster the engine uses.  Submitting a task from inside another task's ``finished()``
callback is what 3.44.13 sometimes takes without ever running it: the task stays
``Queued``, ``finished()`` never comes, and the slot is lost for the session.
The posted drain re-reads the slot count and every waiting record before it starts
anything, so a cancel or a project generation that arrived in between still wins.

QGIS is imported inside ``_build_task_class``, which keeps this module importable
in a bare interpreter like the rest of the core.
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
from ..execution_store import TERMINAL_STATUSES
from ..models import (
    INTERNAL_ERROR,
    STALE_PROJECT,
    TASK_SUBMISSION_FAILED,
    ErrorDetail,
    ExecutionStatus,
    FrameworkError,
    utc_now,
)
from .main_thread import Post, _post_to_event_queue

if TYPE_CHECKING:
    from ..engine import OperationEngine
    from ..execution_store import ExecutionStore

_LOGGER = logging.getLogger(__name__)

# 运行中的任务最多每 0.1 秒改写一次记录。结束前的最后一个值无论如何都会写：
# 在那里节流会让记录落后于后台真正到达的位置。
# How often a running task may rewrite its execution record.  The value reported last before
# the task ends is written regardless: a throttled drop there would leave a finished record
# short of what the worker actually reached.
PROGRESS_INTERVAL_SECONDS = 0.1


class OperationRun:
    """一次后台运行产出的东西，与承载它的 ``QgsTask`` 分开保存。

    ``run`` 从不抛异常：逸进 C++ 任务框架的异常会让 QGIS 中止，而不是让一次执行失败，
    所以它被收下来当诊断，任务报告失败。

    What one worker produced, kept apart from the ``QgsTask`` that carried it.

    ``run`` never raises: an exception escaping into the C++ task framework aborts
    QGIS instead of failing one execution, so it is kept as a diagnostic and the
    task reports failure.
    """

    def __init__(self, operation: BaseOperation, snapshot: Any) -> None:
        self._operation = operation
        self._snapshot = snapshot
        self.result: Any = None
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
    """后台线程对这次执行的全部视野：一个取消标志和进度。

    The worker's whole view of its execution: a cancel flag and progress.
    """

    def __init__(self, task: Any) -> None:
        self._task = task

    def is_cancelled(self) -> bool:
        return bool(self._task.isCanceled())

    def set_progress(self, value: float) -> None:
        # 协议只给 0-100，夹取是适配器的事。
        # The protocol grants 0-100; clamping is the adapter's job.
        self._task.setProgress(max(0.0, min(100.0, float(value))))


@dataclass
class _Submission:
    """一次已接受的后台调用：跑起来需要什么，以及它现在到哪了。

    进度字段放在这里而不是各开一张表，这样关于一次执行的三个事实不会彼此漂移：
    ``progress_written`` 是上次写记录的时刻，``progress_pending`` 是区间内被留下的最新值。

    One accepted background call: what it needs to run, and where it stands.

    The progress fields belong here rather than in maps of their own, so the three
    facts about an execution cannot drift apart: ``progress_written`` is when the
    record was last moved, ``progress_pending`` the newest value kept back inside
    the interval.
    """

    engine: OperationEngine
    execution_id: str
    operation: BaseOperation
    request: BaseModel
    task_id: int = 0
    progress_written: float | None = None
    progress_pending: float | None = None


class OperationTaskRunner:
    """把后台操作提交给 ``QgsTaskManager``，并发上限取自配置。

    Submit background operations to a ``QgsTaskManager``, up to the configured cap.
    """

    def __init__(
        self,
        *,
        task_manager: Any,
        task_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
        post: Post | None = None,
    ) -> None:
        self._new_task = task_factory or task_class()
        self._clock = clock or time.monotonic
        self._post = post or _post_to_event_queue
        self._manager = task_manager
        # 活跃任务按执行 id 索引，等待槽位的调用按提交顺序排队。
        # 工作一结束两边都会空，所以它们都不是历史。
        # Active tasks by execution id, and the calls still waiting for a slot in submission
        # order.  Both are emptied as work finishes, so neither is history.
        self._active: dict[str, _Submission] = {}
        self._waiting: deque[_Submission] = deque()
        task_manager.progressChanged.connect(self._progress_reported)

    def __call__(
        self,
        engine: OperationEngine,
        record: Any,
        operation: BaseOperation,
        request: BaseModel,
    ) -> None:
        """引擎的接缝：一次已接受的后台调用，记录仍是 ``QUEUED``。

        The engine's seam: one accepted background call, still ``QUEUED``.
        """

        submission = _Submission(engine, record.execution_id, operation, request)
        if len(self._active) >= engine._settings.max_active_background:
            # 调用保留它的 QUEUED 记录，直到有槽位空出来。
            # The call keeps its QUEUED record until a slot frees.
            self._waiting.append(submission)
            return
        self._start(submission)

    def cancel(self, engine: OperationEngine, execution_id: str) -> None:
        """请一个任务停下，或者丢掉一个还在等槽位的调用。

        绝不调 ``QgsTaskManager.cancelAll()``：那是整个 QGIS 会话的队列，
        不是这个插件自己的任务。

        Ask one task to stop, or drop one call still waiting for a slot.

        Never ``QgsTaskManager.cancelAll()``: that is the whole QGIS session's queue,
        not this plugin's own tasks.
        """

        submission = self._active.get(execution_id)
        if submission is None:
            # 还在等待队列里的调用没有任务可请：删掉它的条目，
            # 才不会让队列在每次提交加取消之后越来越长。
            # A call still in the waiting deque has no task to ask: removing its entry is what
            # keeps the deque from growing on every submit-and-cancel.
            for waiting in tuple(self._waiting):
                if waiting.execution_id == execution_id:
                    self._waiting.remove(waiting)
            return
        task = self._manager.task(submission.task_id)
        if task is None:
            return
        try:
            task.cancel()
        except Exception:  # noqa: BLE001 - a task QGIS already dropped may not raise here
            _LOGGER.exception("cancel of task %s failed", submission.task_id)

    def task_finished(
        self, execution_id: str, run: OperationRun, task: Any, succeeded: bool
    ) -> None:
        """一个任务在主线程上的完成处理器。

        ``task`` 只在这次调用期间活着，永不被保留：QGIS 在任务一结束时就删掉它的
        C++ 部分。这里无论出什么错，槽位都会被释放，记录也不会停在活跃状态。

        排空是投递出去的，不在这里调用：在这个回调里提交任务，正是 3.44.13 可能接受
        却永不运行的那一种。

        The main-thread completion handler for one task.

        ``task`` is alive for the length of this call and is never kept: QGIS
        deletes its C++ side as soon as the task ends.  Whatever goes wrong in
        here, the slot is released and the record does not stay live.

        The drain is posted, never called here: a submission made inside this
        callback is one 3.44.13 may accept and never run.
        """

        submission = self._active.pop(execution_id, None)
        if submission is None:
            _LOGGER.warning(
                "completion for an execution this runner does not own: %s", execution_id
            )
            return
        try:
            self._complete(submission, run, task, succeeded)
        except Exception:  # noqa: BLE001 - a Qt slot may not raise
            _LOGGER.exception("completion of %s failed", execution_id)
            self._abandon(submission, "the completion of this execution failed")
        finally:
            self._post_drain()

    def _complete(
        self, submission: _Submission, run: OperationRun, task: Any, succeeded: bool
    ) -> None:
        store = submission.engine._store
        execution_id = submission.execution_id
        record = store.get(execution_id)
        cancelled = bool(task.isCanceled()) or record.status is ExecutionStatus.CANCEL_REQUESTED
        pending = submission.progress_pending
        submission.progress_pending = None
        if pending is not None and record.status not in TERMINAL_STATUSES:
            # 后台线程报告的最后一个进度，即使在节流区间内也会被写。
            # The last progress a worker reported is written even inside the interval.
            record = store.transition(execution_id, progress=pending)

        if run.error is not None:
            # 真正的失败优先于取消请求（CANCEL_REQUESTED -> FAILED）。
            # A real failure outranks a cancel request (CANCEL_REQUESTED -> FAILED).
            self._running(store, execution_id, record)
            if isinstance(run.error, FrameworkError):
                # 后台自己的拒绝保留错误码：字段求和会拒绝溢出成 Infinity 的合计，
                # 调用方要看到的是这个码，而不是 INTERNAL_ERROR。
                # A worker's own refusal keeps its code: the field sum refuses a total that
                # overflowed to Infinity, and the caller has to see that code, not INTERNAL_ERROR.
                self._fail(store, execution_id, run.error.code, run.error.message)
            else:
                self._fail(store, execution_id, INTERNAL_ERROR, _unexpected(run.error))
        elif cancelled:
            if record.status in (ExecutionStatus.SCHEDULED, ExecutionStatus.RUNNING):
                # 依赖图层消失时 QGIS 会自己取消任务，
                # 而 SCHEDULED 与 RUNNING 都不能直接到 CANCELLED。
                # QGIS cancels a task on its own when its dependent layer goes away, and neither
                # SCHEDULED nor RUNNING reaches CANCELLED directly.
                store.transition(execution_id, ExecutionStatus.CANCEL_REQUESTED)
            store.transition(execution_id, ExecutionStatus.CANCELLED)
        elif not succeeded:
            self._running(store, execution_id, record)
            self._fail(store, execution_id, INTERNAL_ERROR, "the task ended without a result")
        else:
            self._finish_results(submission, self._running(store, execution_id, record), run.result)

    def _abandon(self, submission: _Submission, message: str) -> None:
        """把一条完成过程出错的记录收尾。

        任务已经没了，还停在活跃状态的记录再也动不了；
        每个非终态都有一条到 ``FAILED`` 的边。

        Settle a record whose completion broke.

        The task is gone, so a record still in a live state could never be moved
        again; every non-terminal status has an edge to FAILED.
        """

        store = submission.engine._store
        try:
            if store.get(submission.execution_id).status not in TERMINAL_STATUSES:
                self._fail(
                    store,
                    submission.execution_id,
                    INTERNAL_ERROR,
                    message,
                )
        except Exception:  # noqa: BLE001 - nothing may escape into the Qt slot
            _LOGGER.exception("could not settle %s", submission.execution_id)

    def _start(self, submission: _Submission) -> None:
        """把一次 ``QUEUED`` 调用带到 ``SCHEDULED``，或者带到说明为什么不能的记录。

        Take one QUEUED call to SCHEDULED, or to the record that says why not.
        """

        engine = submission.engine
        execution_id = submission.execution_id
        operation = submission.operation
        store = engine._store
        if store.get(execution_id).project_epoch != engine._lifecycle.current_epoch:
            # 旧工程下接受的调用，现在连快照都不许读。
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

        try:
            task = self._new_task(self, execution_id, operation, snapshot)
            dependent = _dependent_layers(engine, operation, submission.request)
            if dependent:
                task.setDependentLayers(dependent)
        except Exception as error:  # noqa: BLE001 - a broken dependency is a FAILED record
            _LOGGER.exception("%s: the task could not be built", operation.name)
            self._fail(store, execution_id, INTERNAL_ERROR, _unexpected(error))
            return
        try:
            task_id = self._manager.addTask(task)
        except Exception:  # noqa: BLE001 - a PyQGIS refusal is still a submission failure
            _LOGGER.exception("%s: addTask raised", operation.name)
            task_id = 0
        if not task_id:
            # 管理器没有接手，所以任务对象归我们自己丢掉。
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

    def _post_drain(self) -> None:
        """在守卫之后投递排空，和这里其他 Qt 槽一样。

        排空现在是自己跑的独立回调，所以过去会接住它异常的 ``finished`` 槽
        不在它的栈上了。

        Post the drain behind the guard every other Qt slot here has.

        The drain runs as a callback of its own now, so the ``finished`` slot that
        used to catch its exceptions is no longer on its stack.
        """

        def drain() -> None:
            try:
                self._drain()
            except Exception:  # noqa: BLE001 - a Qt slot may not raise
                _LOGGER.exception("the background queue could not be drained")

        self._post(drain)

    def _drain(self) -> None:
        """有空槽位时按提交顺序启动等待中的调用。

        在释放槽位的那次完成之后一轮事件循环运行。槽位数和每条记录都是在这里读的，
        而不是投递时读，所以这期间被取消的调用会被丢掉，代数已经变了的工程会被拒绝。

        Start waiting calls while a slot is free, in submission order.

        Runs one event-loop turn after the completion that freed the slot.  The slot
        count and each record are read here, not at posting time, so a call cancelled
        meanwhile is dropped and a project generation that moved on is refused.
        """

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
            try:
                self._start(head)
            except Exception as error:  # noqa: BLE001 - a drain defect may not stop the turn
                _LOGGER.exception("%s: the drain could not start it", head.execution_id)
                if head.execution_id not in self._active:
                    # 没人接手这个被弹出的调用，所以收尾是这个循环的责任；
                    # 活跃条目由它自己任务的完成来释放。
                    # Nothing owns the popped call, so settling it is this loop's job; an active
                    # entry is released by the completion of its own task.
                    self._abandon(head, _unexpected(error))

    def _finish_results(self, submission: _Submission, record: Any, output: Any) -> None:
        """校验工程代数、收尾、然后写入结果。

        Validate the project generation, finalise, and write the result.
        """

        engine = submission.engine
        store = engine._store
        execution_id = submission.execution_id
        if record.project_epoch != engine._lifecycle.current_epoch:
            # 旧一代的结果永远不会到达新工程。
            # An old generation's result never reaches the new project.
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
        store.transition(execution_id, ExecutionStatus.SUCCEEDED, result=serialised, progress=100)

    def _progress_reported(self, task_id: int, progress: float) -> None:
        """任务管理器的进度信号，投递时已经在主线程上。

        The manager's progress signal, already delivered on the main thread.
        """

        try:
            self._record_progress(task_id, progress)
        except Exception:  # noqa: BLE001 - a Qt slot may not raise
            _LOGGER.exception("progress for task %s failed", task_id)

    def _record_progress(self, task_id: int, progress: float) -> None:
        if progress >= 100.0:
            # 只有成功那一跳可以写 100：QGIS 对 run() 返回 True 的每个任务都报 100，
            # 被取消和被失败的也算，而只有 SUCCEEDED 能证明工作真的完成了。
            # Only the success transition may write 100: QGIS reports 100 for every task whose
            # run() returned True, cancelled and failed ones included, and nothing but SUCCEEDED
            # can prove the work really finished.
            return
        submission = self._submission_for(task_id)
        if submission is None:
            return
        now = self._clock()
        written = submission.progress_written
        if written is not None and now - written < PROGRESS_INTERVAL_SECONDS:
            # 每个区间只写一次；最新的值留到最后一次刷写时再写。
            # One write per interval; the newest value waits for the flush at the end.
            submission.progress_pending = progress
            return
        store = submission.engine._store
        execution_id = submission.execution_id
        record = store.get(execution_id)
        if record.status in TERMINAL_STATUSES:
            return
        if record.status is ExecutionStatus.SCHEDULED:
            # 第一次报告才告诉主线程任务真的开始了，因为 ``run`` 在另一个线程上。
            # The first report is what tells the main thread the task really started, since
            # ``run`` is on another thread.
            record = store.transition(execution_id, ExecutionStatus.RUNNING)
        # 区间内留下的值比这次旧，结束时再写它会让进度倒退。
        # A value kept back inside the window is older than this one, so writing it at the end
        # would move the record's progress backwards.
        submission.progress_pending = None
        store.transition(execution_id, progress=progress)
        submission.progress_written = now

    def _submission_for(self, task_id: int) -> _Submission | None:
        for submission in self._active.values():
            if submission.task_id == task_id:
                return submission
        return None

    @staticmethod
    def _running(store: ExecutionStore, execution_id: str, record: Any) -> Any:
        """在别的东西写之前，先把 ``SCHEDULED`` 移到 ``RUNNING``。

        Move SCHEDULED to RUNNING before anything else may be written.
        """

        if record.status is ExecutionStatus.SCHEDULED:
            return store.transition(execution_id, ExecutionStatus.RUNNING)
        return record

    @staticmethod
    def _fail(store: ExecutionStore, execution_id: str, code: str, message: str) -> None:
        store.transition(
            execution_id, ExecutionStatus.FAILED, error=ErrorDetail(code=code, message=message)
        )


def _unexpected(error: BaseException) -> str:
    """对调用方安全的消息：只给类型，不给异常自身的文本。

    引擎对自己的路径写同样的字符串；这里重复一份而不导入，是因为它是另一个模块的
    私有名字，而这条规则只有一行。

    A caller-safe message: the type only, never the exception's own text.

    The engine writes the same string for its own paths; it is repeated here rather
    than imported because it is a private name of another module, and the rule is
    one line.
    """

    return f"{type(error).__name__}: unexpected failure"


def _dependent_layers(
    engine: OperationEngine, operation: BaseOperation, request: BaseModel
) -> list[Any]:
    """任务依赖的图层，好让 QGIS 在图层被移除时取消它。

    操作通过声明 ``dependent_layer_ids(request)`` 来选择加入；三个后台阶段是冻结的
    协议，所以依赖是一件可选附加，而不是第四个阶段。

    The layers a task depends on, so QGIS cancels it when one is removed.

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
    """``QgsTask`` 子类，第一次使用时才构建，本模块因此不需要 QGIS 就能导入。

    The ``QgsTask`` subclass, built on first use so this module imports without QGIS.
    """

    global _TASK_CLASS
    if _TASK_CLASS is None:
        _TASK_CLASS = _build_task_class()
    return _TASK_CLASS


def _build_task_class() -> type:
    from qgis.core import QgsTask

    class OperationTask(QgsTask):
        """一次后台操作，作为一个从提交起就归 QGIS 所有的任务。

        One background operation as a task QGIS owns from submission on.
        """

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
            # 永远在主线程、永远在 Qt 槽里：这里的异常会让 QGIS 中止，
            # 而不是让一次执行失败。
            # Always the main thread, always inside a Qt slot: an exception here would abort QGIS
            # instead of failing one execution.
            try:
                self._runner.task_finished(self._execution_id, self._run, self, bool(result))
            except Exception:  # noqa: BLE001 - a Qt slot may not raise
                _LOGGER.exception("%s: completion handler raised", self._execution_id)

    return OperationTask
