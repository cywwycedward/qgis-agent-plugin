"""The record of every asynchronous action, keyed by the id a caller polls.

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
from dataclasses import dataclass
from typing import Any

MAX_RECORDS = 256


class OperationNotFound(Exception):
    """Raised when an operation identifier is not known to this registry.

    The message names the identifier and is what the polling command answers
    with, so it has to read on its own.  ``code`` is the stable half the error
    envelope reports, the same rule ``ActionError`` follows.
    """

    code = "operation_not_found"


@dataclass
class _Operation:
    """One asynchronous action as a polling caller may see it."""

    operation_id: str
    name: str
    terminal_outcome: str | None = None  # None while it runs
    error_code: str | None = None
    error_message: str | None = None
    result: Any = None
    cancel_requested: bool = False
    job: Any = None  # released at the terminal boundary


class OperationRegistry:
    """Hold one record per asynchronous action and decide when each one is over."""

    def __init__(self, max_records: int = MAX_RECORDS) -> None:
        self.max_records = max_records
        self._operations: dict[str, _Operation] = {}

    def start(self, name: str, job: Any) -> dict[str, Any]:
        """Register one operation, start its job, and return the first snapshot.

        The job's ``finished`` signal is the completion boundary: the record reads
        the job's outcome there, which keeps a cancellation from being overwritten
        by the job's own completion report.
        """

        self._evict_oldest_terminal()
        operation = _Operation(f"op_{secrets.token_urlsafe(16)}", name)
        self._operations[operation.operation_id] = operation
        operation.job = job
        job.finished.connect(lambda *_args: self._finished(operation))
        try:
            job.start()
        except Exception:
            # Nothing ran, so there is no operation to poll: the record is finished,
            # dropped, and the failure reaches the caller as the command's own
            # error.  A job that reports anyway finds a record marked over.
            self._terminal(operation, "failed", "start_failed", None)
            del self._operations[operation.operation_id]
            raise
        return self.status(operation.operation_id)

    def cancel(self, operation_id: str) -> dict[str, Any]:
        """Ask the running job to stop; the record decides what the outcome was."""

        operation = self._get(operation_id)
        if operation.terminal_outcome is None:
            operation.cancel_requested = True
            operation.job.cancelWithoutBlocking()
        return self.status(operation_id)

    def status(self, operation_id: str) -> dict[str, Any]:
        """Return one snapshot for the polling caller."""

        operation = self._get(operation_id)
        snapshot: dict[str, Any] = {
            "operation_id": operation.operation_id,
            "name": operation.name,
            "terminal_outcome": operation.terminal_outcome,
            "error_code": operation.error_code,
            "error_message": operation.error_message,
            "cancel_requested": operation.cancel_requested,
            # A job that cannot say how far it has come reports None; once the
            # operation is terminal the job is gone, and so is the question.
            "progress": None if operation.job is None else operation.job.progress,
            "progress_text": None if operation.job is None else operation.job.progress_text,
        }
        if operation.terminal_outcome == "succeeded":
            snapshot["result"] = operation.result
        return snapshot

    def _finished(self, operation: _Operation) -> None:
        """Read the job's outcome at the completion boundary and record it."""

        if operation.terminal_outcome is not None:
            return
        if operation.cancel_requested:
            self._terminal(operation, "cancelled", None, None)
            return

        error_code = getattr(operation.job, "error_code", None)
        if error_code is not None:
            self._terminal(
                operation,
                "failed",
                error_code,
                None,
                getattr(operation.job, "error_message", None),
            )
            return

        self._terminal(operation, "succeeded", None, getattr(operation.job, "result", None))

    def _terminal(
        self,
        operation: _Operation,
        outcome: str,
        error_code: str | None,
        result: Any,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        """Write the one outcome this operation will ever have."""

        if operation.terminal_outcome is not None:
            return self.status(operation.operation_id)
        operation.terminal_outcome = outcome
        operation.error_code = error_code
        operation.error_message = error_message
        operation.result = result
        # Release the job at the callback boundary: QGIS owns it from here on.
        operation.job = None
        return self.status(operation.operation_id)

    def _get(self, operation_id: str) -> _Operation:
        operation = self._operations.get(operation_id)
        if operation is None:
            raise OperationNotFound(f"Unknown operation: {operation_id}")
        return operation

    def _evict_oldest_terminal(self) -> None:
        if len(self._operations) < self.max_records:
            return
        for operation_id, operation in list(self._operations.items()):
            if operation.terminal_outcome is not None:
                del self._operations[operation_id]
                return
        raise RuntimeError("overloaded")
