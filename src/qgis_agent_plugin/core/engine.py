"""框架唯一的入口：一个名字加一个字典变成一条记录。

两类失败被刻意分开。没通过控制面的调用——错线程、引擎已关闭、
未知操作、不支持的执行方式——抛 ``FrameworkError`` 且不留下记录，
因为这次请求根本没有成为一次执行。通过控制面之后才失败的调用，
则以 ``FAILED`` 记录结束，因为它确实成了一次执行。

这里的一切都跑在构建引擎的那个线程上：存储不会从别处被写，
所以状态不会被后台线程挪动。主线程工作交给事件循环延后执行，
而不是在 ``submit`` 里直接跑；后台工作交给注入进来的适配器，
引擎只在需要时请它停下自己还持有的任务。

The single entry point of the framework: a name and a dict become a record.

Two kinds of failure are kept apart on purpose.  A call that fails the
control plane -- wrong thread, closed engine, unknown operation, unsupported
mode -- raises ``FrameworkError`` and leaves no record behind, because the caller's
request never became an execution.  A call that passes the control plane and then
fails ends as a ``FAILED`` record, because it did become one.

Everything here runs on the thread that built the engine: the store is
written from nowhere else, so a status can never be moved by a worker.  Main-thread
work is deferred to the event loop rather than run inside ``submit``; background
work is handed to one injected adapter, and the engine owns nothing of the QgsTask
side of that seam beyond asking it to stop what it still owns.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from functools import partial
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from .config import Settings
from .context import ProjectLifecycle, require_main_thread
from .contracts import BaseOperation, ExecutionStrategy, MainThreadOperationMixin, OperationContext
from .execution_store import TERMINAL_STATUSES, ExecutionStore
from .models import (
    ENGINE_CLOSING,
    INTERNAL_ERROR,
    INVALID_INPUT,
    OUTPUT_VALIDATION_FAILED,
    TASK_SUBMISSION_FAILED,
    TOO_MANY_EXECUTIONS,
    UNSUPPORTED_EXECUTION_MODE,
    CancelReceipt,
    ErrorDetail,
    ExecutionRecord,
    ExecutionStatus,
    FrameworkError,
    utc_now,
)
from .registry import OperationRegistry
from .runners.main_thread import MainThreadRunner

Callback = Callable[[], None]

# 后台适配器的入口：在放行的 ``submit`` 之后一轮、在主线程上调用，
# 此时记录仍是 ``QUEUED``；PREPARING、任务提交与完成回调都归适配器，
# 引擎不碰这些。
# The adapter's entry point, called on the main thread one turn after an accepted background
# ``submit``, with the record still ``QUEUED``.  The adapter owns PREPARING, the QgsTask
# submission and the completion callback; the engine owns none of that.
BackgroundRunner = Callable[["OperationEngine", ExecutionRecord, BaseOperation, BaseModel], None]

# 同一适配器的另一半：请一个任务停下。``cancel``、工程被换掉与 ``shutdown``
# 都要它，而它们都不许碰全局的 cancelAll。
# The second half of the same adapter: ask one task to stop.  ``cancel``, a replaced project
# and ``shutdown`` all need it, and none may reach the global ``QgsTaskManager.cancelAll()``.
BackgroundCanceller = Callable[["OperationEngine", str], None]

_QUEUED = frozenset({ExecutionStatus.QUEUED})

# 计入 ``max_active_background`` 的后台状态，也是工程被取消或被换掉时得请停下的那些。
# The background statuses that count against ``max_active_background`` and that a cancelled or
# replaced project must ask to stop.
_ACTIVE_BACKGROUND = frozenset({ExecutionStatus.SCHEDULED, ExecutionStatus.RUNNING})

_LOGGER = logging.getLogger(__name__)


class OperationEngine:
    """一次插件会话的派发、状态与取消。

    Dispatch, state and cancellation for one plugin session.
    """

    def __init__(
        self,
        registry: OperationRegistry,
        settings: Settings,
        lifecycle: ProjectLifecycle,
        *,
        iface: Any = None,
        project: Any = None,
        background_runner: BackgroundRunner | None = None,
        background_cancel: BackgroundCanceller | None = None,
        post: Callable[[Callback], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._lifecycle = lifecycle
        self._iface = iface
        self._project = project
        self._background_runner = background_runner
        self._background_cancel = background_cancel
        self._store = ExecutionStore(settings, clock=clock)
        self._runner = MainThreadRunner(self._is_live, post=post)
        self._owner_thread = threading.get_ident()
        self._closing = False

    def run_sync(self, name: str, arguments: dict[str, Any]) -> ExecutionRecord:
        """立即跑一个主线程操作，返回它的终态记录。

        Run a main-thread operation now and return its terminal record.
        """

        self._require_open()
        operation = self._registry.get(name)
        if operation.strategy is not ExecutionStrategy.MAIN_THREAD:
            raise FrameworkError(
                UNSUPPORTED_EXECUTION_MODE, f"{name} is a background operation; submit it instead"
            )
        record = self._create(operation)
        try:
            request = self._validated_input(operation, arguments)
        except FrameworkError as error:
            return self._failed(record.execution_id, error.code, error.message)
        return self._execute(record, operation, request)

    def submit(self, name: str, arguments: dict[str, Any]) -> ExecutionRecord:
        """接受一次稍后执行的调用，返回它的 ``QUEUED`` 快照。

        Accept a call for later execution and return its ``QUEUED`` snapshot.
        """

        self._require_open()
        operation = self._registry.get(name)
        record = self._create(operation)
        execution_id = record.execution_id
        # 查操作、查容量、校验输入都在返回之前完成，
        # 所以被接受的调用不会先排队、之后才因参数错误而失败。
        # Lookup, capacity and input validation all happen before this returns, so an
        # accepted call never sits in the queue and then fails for an argument error.
        if len(self._store.ids(_QUEUED)) > self._settings.max_pending:
            return self._failed(
                execution_id, TOO_MANY_EXECUTIONS, f"{name}: too many executions are pending"
            )
        try:
            request = self._validated_input(operation, arguments)
        except FrameworkError as error:
            return self._failed(execution_id, error.code, error.message)
        if operation.strategy is ExecutionStrategy.BACKGROUND:
            if self._background_runner is None:
                return self._failed(
                    execution_id,
                    TASK_SUBMISSION_FAILED,
                    f"{name}: no background runner is installed",
                )
            self._runner.post(
                execution_id, partial(self._start_background, execution_id, operation, request)
            )
        else:
            self._runner.post(
                execution_id, partial(self._run_queued, execution_id, operation, request)
            )
        return self._store.get(execution_id)

    def get(self, execution_id: str) -> ExecutionRecord:
        """一条执行的快照；查不到或已被淘汰的 id 是 ``EXECUTION_NOT_FOUND``。

        ``shutdown`` 之后历史仍然可读：消失的是插件，不是这个会话的记录，
        所以这里只要求主线程，不要求引擎仍然开着。

        One execution's snapshot; a missed or evicted id is ``EXECUTION_NOT_FOUND``.

        History stays readable after ``shutdown``: the plugin is gone, not the
        session's records, so this asks for the main thread but not for an open
        engine.
        """

        self._require_main_thread()
        return self._store.get(execution_id)

    def cancel(self, execution_id: str) -> CancelReceipt:
        """按这条记录的状态与策略请求停止，能否答应取决于状态。

        Ask for a stop, decided by this record's status and strategy.
        """

        self._require_open()
        record = self._store.get(execution_id)
        if record.status in TERMINAL_STATUSES:
            return _receipt(record, accepted=False, reason="ALREADY_TERMINAL")
        if record.status is ExecutionStatus.QUEUED:
            # 排队中的回调会发现记录已被取消，并跳过它。
            # The queued callback will find the record cancelled and skip it.
            cancelled = self._store.transition(execution_id, ExecutionStatus.CANCELLED)
            if record.strategy is ExecutionStrategy.BACKGROUND:
                # 没走到任务的调用仍是适配器队列里的等待项，
                # 只有适配器能把它从那个队列里删掉。
                # A background call that never reached a task is still the adapter's waiting
                # entry, and only the adapter can drop it from that queue.
                self._cancel_task(execution_id)
            return _receipt(cancelled, True)
        if record.status is ExecutionStatus.FINALIZING:
            # 结果正在被应用时到达的取消无法兑现。
            # A cancel that arrives once results are being applied cannot be honoured.
            return _receipt(record, accepted=False, reason="FINALIZING")
        if record.strategy is ExecutionStrategy.BACKGROUND and record.status in (
            ExecutionStatus.SCHEDULED,
            ExecutionStatus.RUNNING,
        ):
            # 只有任务自己的报告才会把请求变成 CANCELLED；
            # 请 QgsTask 停下是适配器的另一半。
            # Only the task's own report turns the request into CANCELLED; asking QgsTask to
            # stop is the adapter's half of this call.
            receipt = self._store.transition(execution_id, ExecutionStatus.CANCEL_REQUESTED)
            self._cancel_task(execution_id)
            return _receipt(receipt, True)
        if record.status is ExecutionStatus.CANCEL_REQUESTED:
            return _receipt(record, accepted=True)
        # 已在运行的主线程操作无法中断，快照正在这里准备的后台操作也一样，
        # 它们只能跑完。
        # A main-thread operation that is already running cannot be interrupted, and neither can
        # a background one whose snapshot is being prepared right here.
        return _receipt(record, accepted=False, reason="NOT_INTERRUPTIBLE")

    def shutdown(self) -> None:
        """先停止接受工作，再取消本会话仍然持有的一切。

        排队中的调用直接取消；QGIS 已经拿着的任务只被请求停下，
        由任务自己的报告结束它。

        Stop accepting work, then cancel everything this session still owns.

        The queued calls are cancelled outright; the tasks QGIS already holds are
        only asked to stop, and their own report is what ends them.
        """

        self._require_main_thread()
        if self._closing:
            return
        self._closing = True
        for execution_id in self._store.ids(_QUEUED):
            record = self._store.transition(execution_id, ExecutionStatus.CANCELLED)
            if record.strategy is ExecutionStrategy.BACKGROUND:
                # 排队的后台调用仍是适配器的等待项，
                # 而本会话关闭之后再没有东西会排空它。
                # A queued background call is still the adapter's waiting entry, and after this
                # session closes nothing would ever drain it.
                self._cancel_task(execution_id)
        self._stop_background()

    def on_project_replaced(self) -> None:
        """丢掉属于旧工程代数的全部东西。

        生命周期在 ``ProjectLifecycle.bump()`` 之后立刻调它（插件把加代数这一步
        挂在 ``QgsProject.aboutToBeCleared`` 上）。旧代数下排队的调用立即取消；
        运行中的后台任务被请求协作式停下，它们之后返回的任何结果在收尾时
        都会被拒（``STALE_PROJECT``）。

        Drop everything that belongs to the old project generation.

        The lifecycle calls this right after ``ProjectLifecycle.bump()`` (the plugin
        binds that to ``QgsProject.aboutToBeCleared``).  Calls queued under the old
        epoch are cancelled at once; running background tasks are asked to stop
        cooperatively, and anything they still return is refused at finalisation
        (``STALE_PROJECT``).
        """

        self._require_main_thread()
        epoch = self._lifecycle.current_epoch
        try:
            for execution_id in self._store.ids(_QUEUED):
                if self._store.get(execution_id).project_epoch != epoch:
                    self._store.transition(execution_id, ExecutionStatus.CANCELLED)
        finally:
            # 代数在这次调用之前已经加过（插件先加），
            # 所以上面的异常也不能留下旧一代没被请求停下的任务。
            # The epoch moved before this call (the plugin bumps it first), so a raise above may
            # not leave the old generation's tasks running un-asked to stop.
            self._stop_background()

    def _stop_background(self) -> None:
        """先登记取消请求，再请适配器停下每个活着的任务。

        只针对后台记录：在主线程里从 ``execute_main`` 内部触发 ``aboutToBeCleared``
        的那次调用（``project.open``、``project.create``）就在这个栈上，
        而已开始的主线程工作既不取消也不等待，只是被允许跑完。

        Report the cancel request, then ask the adapter to stop every live task.

        Only background records: the main-thread call that fires ``aboutToBeCleared``
        from inside ``execute_main`` (``project.open``, ``project.create``) is on this
        very stack, and main-thread work that has already started is neither
        cancelled nor awaited; a running one is only left to finish.
        """

        live = [
            execution_id
            for execution_id in self._store.ids(_ACTIVE_BACKGROUND)
            if self._store.get(execution_id).strategy is ExecutionStrategy.BACKGROUND
        ]
        for execution_id in live:
            self._store.transition(execution_id, ExecutionStatus.CANCEL_REQUESTED)
        for execution_id in live:
            self._cancel_task(execution_id)

    def _cancel_task(self, execution_id: str) -> None:
        """让适配器替一次执行调用 ``QgsTask.cancel()``。

        Let the adapter call ``QgsTask.cancel()`` for one execution.
        """

        stopper = self._background_cancel
        if stopper is None:
            return
        try:
            stopper(self, execution_id)
        except Exception:  # noqa: BLE001 - an unload hook may not raise into unload
            _LOGGER.exception("cancelling background execution %s failed", execution_id)

    def _require_open(self) -> None:
        self._require_main_thread()
        if self._closing:
            raise FrameworkError(ENGINE_CLOSING, "the engine is shutting down")

    def _require_main_thread(self) -> None:
        require_main_thread(self._owner_thread)

    def _is_live(self, execution_id: str) -> bool:
        """投出去的回调是否仍属于一次进行中的执行。

        Whether a posted callback still belongs to an execution in progress.
        """

        try:
            return self._store.get(execution_id).status not in TERMINAL_STATUSES
        except FrameworkError:
            return False

    def _create(self, operation: BaseOperation) -> ExecutionRecord:
        return self._store.create(
            ExecutionRecord(
                execution_id=str(uuid.uuid4()),
                operation=operation.name,
                kind=operation.kind,
                strategy=operation.strategy,
                status=ExecutionStatus.QUEUED,
                created_at=utc_now(),
                project_epoch=self._lifecycle.current_epoch,
            )
        )

    def _context(self) -> OperationContext:
        return OperationContext(
            iface=self._iface,
            project=self._project,
            lifecycle=self._lifecycle,
            policy=self._settings,
        )

    def _validated_input(self, operation: BaseOperation, arguments: dict[str, Any]) -> BaseModel:
        """校验调用方的参数，绝不让任何异常逸出。

        从这里抛出的东西都已经通过控制面，所以它必须变成 ``FAILED`` 记录，
        否则那次调用会永远停在 ``QUEUED`` 并占掉队列里的一个位置。
        模型的校验器抛出 pydantic 没包装的异常，和操作自身的 bug 一样出人意料。

        Validate the caller's arguments, never letting an exception out.

        Anything raised from here has already passed the control plane, so it has to
        become a ``FAILED`` record or that call would stay ``QUEUED`` forever and eat
        one of the queue slots.  A model whose validator raises something pydantic
        does not wrap is as unexpected as an operation's bug.
        """

        try:
            return operation.input_model.model_validate(arguments)
        except FrameworkError:
            # 操作自己的拒绝保留自己的错误码。
            # An operation's own refusal keeps its code.
            raise
        except ValidationError as error:
            raise FrameworkError(
                INVALID_INPUT, f"{operation.name}: invalid input at {_paths(error)}"
            ) from None
        except Exception as error:  # noqa: BLE001 - any other failure is a FAILED record
            _LOGGER.exception("input validation for %s raised", operation.name)
            raise FrameworkError(INTERNAL_ERROR, _unexpected(error)) from error

    def _run_queued(self, execution_id: str, operation: BaseOperation, request: BaseModel) -> None:
        """跑一次排队中的主线程调用，除非它已经不被需要了。

        Run one queued main-thread call, unless it is no longer wanted.
        """

        record = self._store.get(execution_id)
        # runner 已经跳过了已结束的执行；这里问的是更窄的问题（它还在等吗），
        # 这样一个迟到的回调就启不动已经走开的调用。
        # The runner already skipped an execution that ended; this asks the narrower question
        # (is it still waiting?) that keeps a leftover callback from starting a call which has
        # moved on.
        if record.status is not ExecutionStatus.QUEUED:
            return
        if record.project_epoch != self._lifecycle.current_epoch:
            # 调用等待期间工程被换掉，所以它不能碰新工程；
            # 调用方看到的是取消，而不是静默跳过。
            # The project was replaced while the call waited, so it must not touch the new one;
            # the caller sees a cancelled record rather than a silent skip.
            self._store.transition(execution_id, ExecutionStatus.CANCELLED)
            return
        self._execute(record, operation, request)

    def _start_background(
        self, execution_id: str, operation: BaseOperation, request: BaseModel
    ) -> None:
        """把放行的后台调用交给已安装的适配器。

        这就是引擎必须守住的任务提交回调：适配器抛异常不能进到 Qt 槽里，
        调用也不能永远留在队列中。

        Hand an accepted background call to the installed adapter.

        This is the task-submission callback the engine has to guard: an adapter that
        raises must not reach the Qt slot, and the call must not be left queued
        forever.
        """

        runner = self._background_runner
        # 和 runner 问了同一个问题，在交给适配器之前再问一次：
        # 这是把已取消的调用挡在 QgsTask 之外的最后一刻。
        # Same question as the runner's, asked once more right before the adapter gets anything:
        # this is the last point where a cancelled call can be kept away from a QgsTask.
        if runner is None or not self._is_live(execution_id):
            return
        try:
            runner(self, self._store.get(execution_id), operation, request)
        except Exception as error:  # noqa: BLE001 - a submission callback may not raise
            _LOGGER.exception("background runner failed for %s", execution_id)
            if self._is_live(execution_id):
                self._failed(execution_id, INTERNAL_ERROR, _unexpected(error))

    def _execute(
        self, record: ExecutionRecord, operation: BaseOperation, request: BaseModel
    ) -> ExecutionRecord:
        """把一次排队中的主线程调用带到终态记录。

        Take a queued main-thread call to a terminal record.
        """

        execution_id = record.execution_id
        self._store.transition(execution_id, ExecutionStatus.RUNNING, started_at=utc_now())
        try:
            # 注册表已经拒绝过没有 execute_main 的操作，所以这里的转型是安全的。
            # The registry refused any operation without execute_main, so the cast is safe.
            output = cast(MainThreadOperationMixin, operation).execute_main(
                self._context(), request
            )
        except FrameworkError as error:
            # 操作用自己的错误码报告业务失败，这里把码原样留下。
            # An operation reports a business failure with its own code, kept as it is.
            return self._failed(execution_id, error.code, error.message)
        except Exception as error:  # noqa: BLE001 - an operation's bug is a FAILED record
            _LOGGER.exception("operation %s raised", operation.name)
            return self._failed(execution_id, INTERNAL_ERROR, _unexpected(error))
        try:
            result = _serialised_output(operation, output)
        except FrameworkError as error:
            return self._failed(execution_id, error.code, error.message)
        return self._store.transition(
            execution_id, ExecutionStatus.SUCCEEDED, result=result, progress=100
        )

    def _failed(self, execution_id: str, code: str, message: str) -> ExecutionRecord:
        return self._store.transition(
            execution_id,
            ExecutionStatus.FAILED,
            error=ErrorDetail(code=code, message=message),
        )


def _receipt(record: ExecutionRecord, accepted: bool, reason: str | None = None) -> CancelReceipt:
    return CancelReceipt(
        execution_id=record.execution_id,
        accepted=accepted,
        status=record.status,
        reason=reason,
    )


def _serialised_output(operation: BaseOperation, output: Any) -> dict[str, Any]:
    """校验操作返回的东西，然后转成 JSON。

    Validate what the operation returned, then make it JSON.
    """

    try:
        model = operation.output_model.model_validate(output)
        return model.model_dump(mode="json")
    except Exception as error:  # noqa: BLE001 - any unusable output is the same refusal
        raise FrameworkError(
            OUTPUT_VALIDATION_FAILED,
            f"{operation.name}: output does not match {operation.output_model.__name__}",
        ) from error


def _paths(error: ValidationError) -> str:
    """点出出错的字段，但不回显调用方的取值。

    Name the offending fields without echoing the caller's values.
    """

    return ", ".join(
        ".".join(str(part) for part in detail["loc"]) or "<root>" for detail in error.errors()[:3]
    )


def _unexpected(error: Exception) -> str:
    """对调用方安全的消息：只给类型，不给异常自身的文本。

    异常文本可能带路径或 provider URI，而记录要交给调用方，
    所以文本只进本地日志，不进 ``error.message``。

    A caller-safe message: the type only, never the exception's own text.

    An exception text can carry a path or a provider URI, and a record is handed to
    the caller, so the text goes to the local log and not into ``error.message``.
    """

    return f"{type(error).__name__}: unexpected failure"
