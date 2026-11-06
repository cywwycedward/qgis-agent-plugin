"""The operation lifecycle: who owns the terminal outcome, and what a poll reads."""

from __future__ import annotations

import pytest

from qgis_agent_plugin.operation_registry import OperationNotFound, OperationRegistry


class Signal:
    """The hole a job's ``finished`` needs, without Qt: connect and emit."""

    def __init__(self) -> None:
        self._callbacks: list = []

    def connect(self, callback: object) -> None:
        self._callbacks.append(callback)

    def emit(self) -> None:
        for callback in list(self._callbacks):
            callback()


class StubJob:
    """A job the registry can hold: it starts, it answers, it finishes."""

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


def started(registry: OperationRegistry) -> tuple[str, StubJob, dict]:
    """Register one operation and start a job on it, the way ``arun`` does."""

    job = StubJob()
    snapshot = registry.start("run", job)
    return snapshot["operation_id"], job, snapshot


def test_registry_start_answers_a_snapshot_and_starts_the_job() -> None:
    registry = OperationRegistry()
    operation_id, job, snapshot = started(registry)

    assert job.started is True
    assert snapshot["operation_id"] == operation_id
    assert snapshot["terminal_outcome"] is None


def test_registry_records_the_job_result_when_it_finishes() -> None:
    registry = OperationRegistry()
    operation_id, job, _ = started(registry)

    job.result = {"ok": True}
    job.finished.emit()
    snapshot = registry.status(operation_id)

    assert snapshot["terminal_outcome"] == "succeeded"
    assert snapshot["result"] == {"ok": True}
    # The job is released at the callback boundary, so nothing polls it afterwards.
    assert snapshot["progress"] is None


def test_registry_reports_the_progress_of_a_running_job() -> None:
    registry = OperationRegistry()
    operation_id, job, _ = started(registry)

    job.progress = 42.0
    job.progress_text = "Transformed 4 of 5"
    snapshot = registry.status(operation_id)

    assert snapshot["progress"] == 42.0
    assert snapshot["progress_text"] == "Transformed 4 of 5"


def test_registry_cancel_is_not_overwritten_by_the_job_finishing() -> None:
    registry = OperationRegistry()
    operation_id, job, _ = started(registry)

    registry.cancel(operation_id)
    assert job.cancelled is True

    # A cancelled job still reports success; the registry is what decides.
    job.result = {"ok": True}
    job.finished.emit()
    snapshot = registry.status(operation_id)

    assert snapshot["terminal_outcome"] == "cancelled"
    assert "result" not in snapshot


def test_registry_cancel_of_a_finished_operation_keeps_its_outcome() -> None:
    registry = OperationRegistry()
    operation_id, job, _ = started(registry)
    job.finished.emit()

    snapshot = registry.cancel(operation_id)

    assert snapshot["terminal_outcome"] == "succeeded"
    assert snapshot["cancel_requested"] is False


def test_registry_operation_failed_by_its_job_reports_the_job_message() -> None:
    registry = OperationRegistry()
    operation_id, job, _ = started(registry)

    job.error_code = "processing_failed"
    job.error_message = "Could not write output"
    job.finished.emit()
    snapshot = registry.status(operation_id)

    assert snapshot["terminal_outcome"] == "failed"
    assert snapshot["error_code"] == "processing_failed"
    assert snapshot["error_message"] == "Could not write output"


def test_registry_job_that_cannot_start_raises_and_ignores_a_late_report() -> None:
    registry = OperationRegistry()

    job = JobThatCannotStart()
    with pytest.raises(RuntimeError):
        registry.start("run", job)

    # Nothing ran, so there is no operation to answer for; a job that reports
    # anyway must not raise from inside the signal (that would abort QGIS).
    job.finished.emit()


def test_registry_unknown_operation_raises_operation_not_found() -> None:
    with pytest.raises(OperationNotFound) as raised:
        OperationRegistry().status("op_missing")

    assert raised.value.code == "operation_not_found"


def test_registry_full_of_records_evicts_the_oldest_terminal_one() -> None:
    registry = OperationRegistry(max_records=2)
    kept = registry.start("a", StubJob())["operation_id"]
    evicted_job = StubJob()
    evicted = registry.start("b", evicted_job)["operation_id"]
    evicted_job.finished.emit()

    newest = registry.start("c", StubJob())["operation_id"]

    with pytest.raises(OperationNotFound):
        registry.status(evicted)
    assert registry.status(kept)["terminal_outcome"] is None
    assert registry.status(newest)["terminal_outcome"] is None
