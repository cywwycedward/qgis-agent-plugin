"""The single entry point of the framework: a name and a dict become a record.

Two kinds of failure are kept apart on purpose (PRD 6.5).  A call that fails the
control plane -- wrong thread, closed engine, unknown operation, unsupported
mode -- raises ``FrameworkError`` and leaves no record behind, because the caller's
request never became an execution.  A call that passes the control plane and then
fails ends as a ``FAILED`` record, because it did become one.

Everything here runs on the thread that built the engine (PRD 8.3): the store is
written from nowhere else, so a status can never be moved by a worker.  Main-thread
work is deferred to the event loop rather than run inside ``submit``; background
work is handed to one injected adapter, and F2 owns nothing of the QgsTask side of
that seam.
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

# What F5 injects: called on the main thread, one turn after an accepted background
# ``submit``, with the record still ``QUEUED``.  The adapter owns PREPARING, the
# QgsTask submission and the completion callback; F2 owns none of that.
BackgroundRunner = Callable[
    ["OperationEngine", ExecutionRecord, BaseOperation, BaseModel], None
]

_QUEUED = frozenset({ExecutionStatus.QUEUED})

_LOGGER = logging.getLogger(__name__)


class OperationEngine:
    """Dispatch, state and cancellation for one plugin session (PRD 6.5)."""

    def __init__(
        self,
        registry: OperationRegistry,
        settings: Settings,
        lifecycle: ProjectLifecycle,
        *,
        iface: Any = None,
        project: Any = None,
        background_runner: BackgroundRunner | None = None,
        post: Callable[[Callback], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._lifecycle = lifecycle
        self._iface = iface
        self._project = project
        self._background_runner = background_runner
        self._store = ExecutionStore(settings, clock=clock)
        self._runner = MainThreadRunner(self._is_live, post=post)
        self._owner_thread = threading.get_ident()
        self._closing = False

    def run_sync(self, name: str, arguments: dict[str, Any]) -> ExecutionRecord:
        """Run a main-thread operation now and return its terminal record."""

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
        """Accept a call for later execution and return its ``QUEUED`` snapshot."""

        self._require_open()
        operation = self._registry.get(name)
        record = self._create(operation)
        execution_id = record.execution_id
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
        """One execution's snapshot; a missed or evicted id is ``EXECUTION_NOT_FOUND``.

        History stays readable after ``shutdown``: the plugin is gone, not the
        session's records (PRD 6.6), so this asks for the main thread but not for
        an open engine.
        """

        self._require_main_thread()
        return self._store.get(execution_id)

    def cancel(self, execution_id: str) -> CancelReceipt:
        """Ask for a stop, per the PRD 6.5 cell for this status and strategy."""

        self._require_open()
        record = self._store.get(execution_id)
        if record.status in TERMINAL_STATUSES:
            return _receipt(record, accepted=False, reason="ALREADY_TERMINAL")
        if record.status is ExecutionStatus.QUEUED:
            # The queued callback will find the record cancelled and skip it (PRD 8.5).
            return _receipt(self._store.transition(execution_id, ExecutionStatus.CANCELLED), True)
        if record.status is ExecutionStatus.FINALIZING:
            # A cancel that arrives once results are being applied cannot be honoured.
            return _receipt(record, accepted=False, reason="FINALIZING")
        if record.strategy is ExecutionStrategy.BACKGROUND and record.status in (
            ExecutionStatus.SCHEDULED,
            ExecutionStatus.RUNNING,
        ):
            # F5 calls QgsTask.cancel() on this status; only the task's own report
            # turns the request into CANCELLED (PRD 8.5).
            return _receipt(
                self._store.transition(execution_id, ExecutionStatus.CANCEL_REQUESTED), True
            )
        if record.status is ExecutionStatus.CANCEL_REQUESTED:
            return _receipt(record, accepted=True)
        # A main-thread operation that is already running cannot be interrupted, and
        # neither can a background one whose snapshot is being prepared right here.
        return _receipt(record, accepted=False, reason="NOT_INTERRUPTIBLE")

    def shutdown(self) -> None:
        """Stop accepting work, then cancel everything that never started (PRD 9.3)."""

        self._require_main_thread()
        if self._closing:
            return
        self._closing = True
        for execution_id in self._store.ids(_QUEUED):
            self._store.transition(execution_id, ExecutionStatus.CANCELLED)

    def _require_open(self) -> None:
        self._require_main_thread()
        if self._closing:
            raise FrameworkError(ENGINE_CLOSING, "the engine is shutting down")

    def _require_main_thread(self) -> None:
        require_main_thread(self._owner_thread)

    def _is_live(self, execution_id: str) -> bool:
        """Whether a posted callback still belongs to an execution in progress."""

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

    def _validated_input(
        self, operation: BaseOperation, arguments: dict[str, Any]
    ) -> BaseModel:
        try:
            return operation.input_model.model_validate(arguments)
        except ValidationError as error:
            raise FrameworkError(
                INVALID_INPUT, f"{operation.name}: invalid input at {_paths(error)}"
            ) from None

    def _run_queued(
        self, execution_id: str, operation: BaseOperation, request: BaseModel
    ) -> None:
        """Run one queued main-thread call, unless it is no longer wanted."""

        record = self._store.get(execution_id)
        if record.status is not ExecutionStatus.QUEUED:
            return
        if record.project_epoch != self._lifecycle.current_epoch:
            # The project was replaced while the call waited, so it must not touch the
            # new one; the caller sees a cancelled record rather than a silent skip.
            self._store.transition(execution_id, ExecutionStatus.CANCELLED)
            return
        self._execute(record, operation, request)

    def _start_background(
        self, execution_id: str, operation: BaseOperation, request: BaseModel
    ) -> None:
        """Hand an accepted background call to the installed adapter (F5).

        This is the task-submission callback PRD 11.3 requires the engine to guard:
        an adapter that raises must not reach the Qt slot, and the call must not be
        left queued forever.
        """

        runner = self._background_runner
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
        """Take a queued main-thread call to a terminal record."""

        execution_id = record.execution_id
        self._store.transition(execution_id, ExecutionStatus.RUNNING, started_at=utc_now())
        try:
            # The registry refused any operation without execute_main (PRD 6.2).
            output = cast(MainThreadOperationMixin, operation).execute_main(
                self._context(), request
            )
        except FrameworkError as error:
            # An operation reports a business failure with a PRD 11.2 code.
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


def _receipt(
    record: ExecutionRecord, accepted: bool, reason: str | None = None
) -> CancelReceipt:
    return CancelReceipt(
        execution_id=record.execution_id,
        accepted=accepted,
        status=record.status,
        reason=reason,
    )


def _serialised_output(operation: BaseOperation, output: Any) -> dict[str, Any]:
    """Validate what the operation returned, then make it JSON (PRD 6.1)."""

    try:
        model = operation.output_model.model_validate(output)
        return model.model_dump(mode="json")
    except Exception as error:  # noqa: BLE001 - any unusable output is the same refusal
        raise FrameworkError(
            OUTPUT_VALIDATION_FAILED,
            f"{operation.name}: output does not match {operation.output_model.__name__}",
        ) from error


def _paths(error: ValidationError) -> str:
    """Name the offending fields without echoing the caller's values (PRD 11.3)."""

    return ", ".join(
        ".".join(str(part) for part in detail["loc"]) or "<root>"
        for detail in error.errors()[:3]
    )


def _unexpected(error: Exception) -> str:
    """A caller-safe message: the type only, never the exception's own text.

    An exception text can carry a path or a provider URI, and a record is handed to
    the caller, so the text goes to the local log and not into ``error.message``
    (PRD 11.1, 11.3).
    """

    return f"{type(error).__name__}: unexpected failure"
