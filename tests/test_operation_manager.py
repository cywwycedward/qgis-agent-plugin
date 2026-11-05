"""The operation lifecycle: who owns the terminal outcome, and what a poll reads."""

from __future__ import annotations

import pytest

from qgis_agent_plugin.qgis_operation_manager import OperationNotFound, QGISOperationManager


class Signal:
    """The hole a job's ``finished`` needs, without Qt: connect, disconnect, emit."""

    def __init__(self) -> None:
        self._callbacks: list = []

    def connect(self, callback: object) -> None:
        self._callbacks.append(callback)

    def disconnect(self, callback: object) -> None:
        self._callbacks.remove(callback)

    def emit(self) -> None:
        for callback in list(self._callbacks):
            callback()


class StubJob:
    """A job the manager can hold: it starts, it answers, it finishes."""

    def __init__(self) -> None:
        self.finished = Signal()
        self.result: object = None
        self.error_code: str | None = None
        self.error_message: str | None = None
        self.progress: float | None = None
        self.progress_text: str | None = None
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancelWithoutBlocking(self) -> None:
        self.cancelled = True


class JobThatCannotStart(StubJob):
    def start(self) -> None:
        raise RuntimeError("no worker")


def started(manager: QGISOperationManager) -> tuple[str, StubJob]:
    """Register one operation and start a job on it, the way ``arun`` does."""

    operation_id = manager.enqueue("run")
    job = StubJob()
    manager.start(operation_id, job)
    return operation_id, job


def test_manager_started_operation_reports_running_then_the_job_result() -> None:
    manager = QGISOperationManager()
    operation_id, job = started(manager)

    assert manager.status(operation_id)["lifecycle_state"] == "running"

    job.result = {"ok": True}
    job.finished.emit()
    snapshot = manager.status(operation_id)

    assert snapshot["terminal_outcome"] == "succeeded"
    assert snapshot["result"] == {"ok": True}
    # The job is released at the callback boundary, so nothing polls it afterwards.
    assert snapshot["progress"] is None


def test_manager_cancel_is_not_overwritten_by_the_job_finishing() -> None:
    manager = QGISOperationManager()
    operation_id, job = started(manager)

    manager.cancel(operation_id)
    assert job.cancelled is True

    # A cancelled job still reports success; the manager is what decides.
    job.result = {"ok": True}
    job.finished.emit()
    snapshot = manager.status(operation_id)

    assert snapshot["terminal_outcome"] == "cancelled"
    assert "result" not in snapshot


def test_manager_cancel_of_a_queued_operation_never_lets_it_start() -> None:
    manager = QGISOperationManager()
    operation_id = manager.enqueue("run")

    assert manager.cancel(operation_id)["terminal_outcome"] == "cancelled"
    with pytest.raises(RuntimeError):
        manager.start(operation_id, StubJob())


def test_manager_operation_failed_by_its_job_reports_the_job_message() -> None:
    manager = QGISOperationManager()
    operation_id, job = started(manager)

    job.error_code = "processing_failed"
    job.error_message = "Could not write output"
    job.finished.emit()
    snapshot = manager.status(operation_id)

    assert snapshot["terminal_outcome"] == "failed"
    assert snapshot["error_code"] == "processing_failed"
    assert snapshot["error_message"] == "Could not write output"


def test_manager_job_that_cannot_start_is_recorded_as_failed() -> None:
    manager = QGISOperationManager()
    operation_id = manager.enqueue("run")

    with pytest.raises(RuntimeError):
        manager.start(operation_id, JobThatCannotStart())
    snapshot = manager.status(operation_id)

    assert snapshot["terminal_outcome"] == "failed"
    assert snapshot["error_code"] == "start_failed"


def test_manager_unknown_operation_raises_operation_not_found() -> None:
    with pytest.raises(OperationNotFound) as raised:
        QGISOperationManager().status("op_missing")

    assert raised.value.code == "operation_not_found"


def test_manager_full_of_records_evicts_the_oldest_terminal_one() -> None:
    manager = QGISOperationManager(max_records=2)
    kept = manager.enqueue("a")
    evicted = manager.enqueue("b")
    manager.cancel(evicted)

    newest = manager.enqueue("c")

    with pytest.raises(OperationNotFound):
        manager.status(evicted)
    assert manager.status(kept)["lifecycle_state"] == "queued"
    assert manager.status(newest)["lifecycle_state"] == "queued"
