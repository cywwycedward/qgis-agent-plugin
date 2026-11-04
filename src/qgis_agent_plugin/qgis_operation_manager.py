"""Operation records for actions that run asynchronously.

The manager holds one record per asynchronous action and is the only writer of
a terminal outcome.  It knows nothing about QGIS: the job is handed in at
``start`` and reports back through its ``finished`` signal, so the whole
lifecycle is testable with a stub job.

Everything here runs on the QGIS main thread -- the bridge's poll loop calls
``enqueue``, and ``finished`` is delivered by a queued connection -- so the
records need no lock.  That assumption is what a background socket reader
would break.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MAX_RECORDS = 256


class OperationNotFound(Exception):
    """Raised when an operation identifier is not known to this manager.

    The message names the identifier and is what the polling command answers
    with, so it has to read on its own.  ``code`` is the stable half the error
    envelope reports, the same rule ``ActionError`` follows.
    """

    code = "operation_not_found"


@dataclass
class _Operation:
    operation_id: str
    name: str
    lifecycle_state: str = "queued"  # queued -> running -> terminal
    terminal_outcome: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    result: Any = None
    cancel_requested: bool = False
    job: Any = None
    finished_signal: Any = None
    finished_callback: Callable[..., None] | None = None


class QGISOperationManager:
    """Own the lifecycle of every asynchronous action, and nothing else."""

    def __init__(self, max_records: int = MAX_RECORDS) -> None:
        self.max_records = max_records
        self._operations: dict[str, _Operation] = {}

    def enqueue(self, name: str) -> str:
        """Register one accepted action and return its opaque identifier."""

        self._evict_oldest_terminal()
        operation_id = f"op_{secrets.token_urlsafe(16)}"
        self._operations[operation_id] = _Operation(operation_id, name)
        return operation_id

    def start(
        self,
        operation_id: str,
        job: Any,
    ) -> dict[str, Any]:
        """Attach a QGIS job; its ``finished`` signal is the completion boundary.

        The manager reads the job's outcome at that boundary and is the only
        code that writes the operation's terminal state.  This keeps cancellation
        from being overwritten by a job-specific completion callback.
        """

        operation = self._get(operation_id)
        if operation.lifecycle_state != "queued":
            raise RuntimeError("operation is not queued")
        if operation.cancel_requested:
            raise RuntimeError("operation was cancelled")

        def finished(*_args: Any) -> None:
            self._finished(operation_id)

        operation.job = job
        operation.finished_signal = job.finished
        operation.finished_callback = finished
        job.finished.connect(finished)
        operation.lifecycle_state = "running"
        try:
            job.start()
        except Exception:
            # A job that cannot start must never leave a record stuck in running.
            self._terminal(operation, "failed", "start_failed", None)
            raise
        return self.status(operation_id)

    def complete(self, operation_id: str, result: Any = None) -> dict[str, Any]:
        """Record a successful terminal outcome for an operation."""

        operation = self._get(operation_id)
        if operation.cancel_requested:
            return self._terminal(operation, "cancelled", None, None)
        return self._terminal(operation, "succeeded", None, result)

    def fail(
        self, operation_id: str, error_code: str, error_message: str | None = None
    ) -> dict[str, Any]:
        """Record a failed terminal outcome for an operation.

        ``error_message`` is what the work itself said about the failure.  It is
        kept so that a poll reads why the operation failed instead of one bare
        code that names the step and not the reason.
        """

        operation = self._get(operation_id)
        if operation.cancel_requested:
            return self._terminal(operation, "cancelled", None, None)
        return self._terminal(operation, "failed", error_code, None, error_message)

    def cancel(self, operation_id: str) -> dict[str, Any]:
        """Ask a running job to stop; a queued one is cancelled outright."""

        operation = self._get(operation_id)
        if operation.lifecycle_state == "terminal":
            return self.status(operation_id)
        if operation.job is None:
            # No job is attached, so cancellation is already proved and this
            # operation must never start later.
            return self._terminal(operation, "cancelled", None, None)
        operation.cancel_requested = True
        operation.job.cancelWithoutBlocking()
        return self.status(operation_id)

    def status(self, operation_id: str) -> dict[str, Any]:
        """Return one snapshot for the polling caller."""

        operation = self._get(operation_id)
        snapshot: dict[str, Any] = {
            "operation_id": operation.operation_id,
            "name": operation.name,
            "lifecycle_state": operation.lifecycle_state,
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

    def _finished(self, operation_id: str) -> None:
        operation = self._operations.get(operation_id)
        if operation is None or operation.lifecycle_state == "terminal":
            return

        job = operation.job
        if operation.cancel_requested:
            self._terminal(operation, "cancelled", None, None)
            return

        error_code = getattr(job, "error_code", None)
        if error_code is not None:
            self._terminal(
                operation,
                "failed",
                error_code,
                None,
                getattr(job, "error_message", None),
            )
            return

        self._terminal(
            operation, "succeeded", None, getattr(job, "result", None)
        )

    def _terminal(
        self,
        operation: _Operation,
        outcome: str,
        error_code: str | None,
        result: Any,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        """Write the one outcome this operation will ever have."""

        if operation.lifecycle_state == "terminal":
            return self.status(operation.operation_id)
        operation.lifecycle_state = "terminal"
        operation.terminal_outcome = outcome
        operation.error_code = error_code
        operation.error_message = error_message
        operation.result = result
        disconnect = getattr(operation.finished_signal, "disconnect", None)
        if callable(disconnect) and operation.finished_callback is not None:
            try:
                disconnect(operation.finished_callback)
            except (TypeError, RuntimeError):
                pass
        # Release the job at the callback boundary: QGIS owns it from here on.
        operation.job = None
        operation.finished_signal = None
        operation.finished_callback = None
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
            if operation.lifecycle_state == "terminal":
                del self._operations[operation_id]
                return
        raise RuntimeError("overloaded")
