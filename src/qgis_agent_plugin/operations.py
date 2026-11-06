"""The record of every asynchronous command, keyed by the id a caller polls.

A record holds what a caller may ask about later -- the outcome, the result, the
reason a run failed -- plus, while the run lasts, the job that is doing it.  It is
the only writer of a terminal outcome, so a cancelled job reporting its own
failure cannot overwrite the cancellation.

Nothing here knows about QGIS: the job arrives at ``start`` and reports back
through its ``finished`` signal, which keeps the whole lifecycle testable with a
stub job.  Everything runs on the QGIS main thread, so the records need no lock;
a background socket reader would break that.
"""

from __future__ import annotations

import secrets
from typing import Any, Literal, Protocol

from pydantic import Field, computed_field

from .command import Result
from .protocol import CommandError

MAX_RECORDS = 256


class Job(Protocol):
    """What ``Operations`` needs from one running job, and nothing more.

    The job is the adapter: a rendering job and a processing task differ in
    everything -- how they start, how they report progress, what Qt mechanism
    tells them they are done -- and this is the whole of what the store sees.
    """

    finished: Any  # a Qt signal: connect(callback) fires on the main thread

    def start(self) -> None: ...

    def cancel(self) -> None: ...

    @property
    def progress(self) -> float | None: ...

    @property
    def progress_text(self) -> str | None: ...

    def outcome(self) -> tuple[Any, CommandError | None]:
        """The wire-ready result and the failure, mutually exclusive."""
        ...


class OperationNotFound(CommandError):
    """Raised when an operation identifier is not known to this registry."""

    def __init__(self, operation_id: str) -> None:
        super().__init__("operation_not_found", f"Unknown operation: {operation_id}")


class Operation(Result):
    """One asynchronous command as a polling caller may see it.

    This is both the record the store keeps and the answer ``get_operation``
    returns, so there is no second type to keep in step: ``job`` is excluded from
    the dump, ``progress`` and ``progress_text`` are read off the running job,
    and every other field is what the store last wrote.
    """

    operation_id: str
    name: str
    terminal_outcome: Literal["succeeded", "failed", "cancelled"] | None = None
    error_code: str | None = None
    error_message: str | None = None
    result: Any = None
    cancel_requested: bool = False
    job: Any = Field(default=None, exclude=True)

    @computed_field
    @property
    def progress(self) -> float | None:
        """How far the job has come, 0-100, or None when it cannot say."""

        return None if self.job is None else self.job.progress

    @computed_field
    @property
    def progress_text(self) -> str | None:
        """What the job last said it was doing, or None when it cannot say."""

        return None if self.job is None else self.job.progress_text


class Operations:
    """Hold one record per asynchronous command and decide when each one is over."""

    def __init__(self, max_records: int = MAX_RECORDS) -> None:
        self.max_records = max_records
        self._operations: dict[str, Operation] = {}

    def start(self, name: str, job: Job) -> Operation:
        """Register one operation, start its job, and return the record.

        The job's ``finished`` signal is the completion boundary: the record reads
        the job's outcome there, which keeps a cancellation from being overwritten
        by the job's own completion report.
        """

        self._evict_oldest_terminal()
        operation = Operation(operation_id=f"op_{secrets.token_urlsafe(16)}", name=name, job=job)
        self._operations[operation.operation_id] = operation
        job.finished.connect(lambda *_args: self._finish(operation))
        try:
            job.start()
        except Exception as error:
            # Nothing ran, so there is no operation to poll: the record is finished
            # and dropped, and the failure reaches the caller as the command's own
            # error.  A job that reports anyway finds a record marked over.
            self._terminal(operation, "failed", CommandError("start_failed", str(error)))
            del self._operations[operation.operation_id]
            raise
        return operation

    def cancel(self, operation_id: str) -> Operation:
        """Ask the running job to stop; the record decides what the outcome was."""

        operation = self._get(operation_id)
        if operation.terminal_outcome is None:
            operation.cancel_requested = True
            operation.job.cancel()
        return operation

    def status(self, operation_id: str) -> Operation:
        """Return one record for the polling caller."""

        return self._get(operation_id)

    def _finish(self, operation: Operation) -> None:
        """Read the job's outcome at the completion boundary and record it."""

        if operation.terminal_outcome is not None:
            return
        if operation.cancel_requested:
            self._terminal(operation, "cancelled")
            return
        result, error = operation.job.outcome()
        if error is not None:
            self._terminal(operation, "failed", error)
            return
        self._terminal(operation, "succeeded", result=result)

    def _terminal(
        self,
        operation: Operation,
        outcome: Literal["succeeded", "failed", "cancelled"],
        error: CommandError | None = None,
        result: Any = None,
    ) -> None:
        """Write the one outcome this operation will ever have."""

        if operation.terminal_outcome is not None:
            return
        operation.terminal_outcome = outcome
        operation.error_code = None if error is None else error.code
        operation.error_message = None if error is None else str(error)
        operation.result = result
        # Release the job at the callback boundary: QGIS owns it from here on.
        operation.job = None

    def _get(self, operation_id: str) -> Operation:
        operation = self._operations.get(operation_id)
        if operation is None:
            raise OperationNotFound(operation_id)
        return operation

    def _evict_oldest_terminal(self) -> None:
        if len(self._operations) < self.max_records:
            return
        for operation_id, operation in list(self._operations.items()):
            if operation.terminal_outcome is not None:
                del self._operations[operation_id]
                return
        raise CommandError(
            "overloaded",
            f"{self.max_records} operations are still running; poll and finish some first.",
        )
