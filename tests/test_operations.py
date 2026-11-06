"""The operation lifecycle: who owns the terminal outcome, and what a poll reads."""

from __future__ import annotations

import pytest

from qgis_agent_plugin.operations import OperationNotFound, Operations
from qgis_agent_plugin.protocol import CommandError


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
    """A job the store can hold: it starts, it answers, it finishes."""

    def __init__(self) -> None:
        self.finished = Signal()
        self.result: object = None
        self.error: CommandError | None = None
        self.progress: float | None = None
        self.progress_text: str | None = None
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def outcome(self) -> tuple[object, CommandError | None]:
        return self.result, self.error


class JobThatCannotStart(StubJob):
    def start(self) -> None:
        raise RuntimeError("no worker")


def started(registry: Operations) -> tuple[str, StubJob]:
    """Register one operation and start a job on it, the way ``arun`` does."""

    job = StubJob()
    operation = registry.start("run", job)
    return operation.operation_id, job


def test_operations_start_answers_a_record_and_starts_the_job() -> None:
    registry = Operations()
    operation_id, job = started(registry)

    assert job.started is True
    assert registry.status(operation_id).terminal_outcome is None


def test_operations_records_the_job_result_when_it_finishes() -> None:
    registry = Operations()
    operation_id, job = started(registry)

    job.result = {"ok": True}
    job.finished.emit()
    operation = registry.status(operation_id)

    assert operation.terminal_outcome == "succeeded"
    assert operation.result == {"ok": True}
    # The job is released at the callback boundary, so nothing polls it afterwards.
    assert operation.job is None


def test_operations_reports_the_progress_of_a_running_job() -> None:
    registry = Operations()
    operation_id, job = started(registry)

    job.progress = 42.0
    job.progress_text = "Transformed 4 of 5"
    operation = registry.status(operation_id)

    assert operation.progress == 42.0
    assert operation.progress_text == "Transformed 4 of 5"


def test_operations_cancel_is_not_overwritten_by_the_job_finishing() -> None:
    registry = Operations()
    operation_id, job = started(registry)

    registry.cancel(operation_id)
    assert job.cancelled is True

    # A cancelled job still reports success; the store is what decides.
    job.result = {"ok": True}
    job.finished.emit()
    operation = registry.status(operation_id)

    assert operation.terminal_outcome == "cancelled"
    assert operation.result is None


def test_operations_cancel_of_a_finished_operation_keeps_its_outcome() -> None:
    registry = Operations()
    operation_id, job = started(registry)
    job.finished.emit()

    operation = registry.cancel(operation_id)

    assert operation.terminal_outcome == "succeeded"
    assert operation.cancel_requested is False


def test_operations_failure_by_its_job_reports_the_job_message() -> None:
    registry = Operations()
    operation_id, job = started(registry)

    job.error = CommandError("processing_failed", "Could not write output")
    job.finished.emit()
    operation = registry.status(operation_id)

    assert operation.terminal_outcome == "failed"
    assert operation.error_code == "processing_failed"
    assert operation.error_message == "Could not write output"


def test_operations_job_that_cannot_start_raises_and_ignores_a_late_report() -> None:
    registry = Operations()

    job = JobThatCannotStart()
    with pytest.raises(RuntimeError):
        registry.start("run", job)

    # Nothing ran, so there is no operation to answer for; a job that reports
    # anyway must not raise from inside the signal (that would abort QGIS).
    job.finished.emit()


def test_operations_unknown_operation_raises_operation_not_found() -> None:
    with pytest.raises(OperationNotFound) as raised:
        Operations().status("op_missing")

    assert raised.value.code == "operation_not_found"


def test_operations_full_of_records_evicts_the_oldest_terminal_one() -> None:
    registry = Operations(max_records=2)
    kept = registry.start("a", StubJob()).operation_id
    evicted_job = StubJob()
    evicted = registry.start("b", evicted_job).operation_id
    evicted_job.finished.emit()

    newest = registry.start("c", StubJob()).operation_id

    with pytest.raises(OperationNotFound):
        registry.status(evicted)
    assert registry.status(kept).terminal_outcome is None
    assert registry.status(newest).terminal_outcome is None


def test_operations_record_is_the_wire_answer() -> None:
    """One type: the record a poll reads is the JSON a caller receives."""

    registry = Operations()
    operation_id, _job = started(registry)

    answer = registry.status(operation_id).model_dump(mode="json")

    assert answer["operation_id"] == operation_id
    assert answer["terminal_outcome"] is None
    assert answer["progress"] is None
    # The job is ours, not the wire's.
    assert "job" not in answer
