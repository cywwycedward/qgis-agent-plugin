"""Execution records, the error contract and the input model rules."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import StrictBool, ValidationError

from qgis_agent_plugin.core.models import (
    ERROR_CODES,
    INTERNAL_ERROR,
    LAYER_NOT_FOUND,
    CancelReceipt,
    ErrorDetail,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionStrategy,
    FrameworkError,
    OperationKind,
    OperationSpec,
    Params,
    utc_now,
)

# PRD 11.2, one entry per row; the set is the whole vocabulary.
PRD_ERROR_CODES = {
    "OPERATION_NOT_FOUND",
    "INVALID_OPERATION_DEFINITION",
    "DUPLICATE_OPERATION",
    "WRONG_THREAD",
    "ENGINE_CLOSING",
    "UNSUPPORTED_EXECUTION_MODE",
    "EXECUTION_NOT_FOUND",
    "INVALID_INPUT",
    "INVALID_CRS",
    "PROJECT_REPLACE_REQUIRED",
    "PROJECT_OPEN_FAILED",
    "PROJECT_SAVE_FAILED",
    "PROJECT_PATH_REQUIRED",
    "TARGET_EXISTS",
    "PATH_NOT_ALLOWED",
    "INVALID_LAYER",
    "LAYER_NOT_FOUND",
    "LAYER_NOT_IN_TREE",
    "INVALID_FIELD_TYPE",
    "SNAPSHOT_LIMIT_EXCEEDED",
    "TOO_MANY_EXECUTIONS",
    "TASK_SUBMISSION_FAILED",
    "STALE_PROJECT",
    "OUTPUT_VALIDATION_FAILED",
    "INTERNAL_ERROR",
}


class StrictParams(Params):
    """The model shape every operation input has to accept."""

    limit: int = 0
    enabled: StrictBool = False


def record(**overrides: object) -> ExecutionRecord:
    values: dict[str, object] = {
        "execution_id": "0f8fad5b-d9cb-469f-a165-70867728950e",
        "operation": "layer.list",
        "kind": OperationKind.QUERY,
        "strategy": ExecutionStrategy.MAIN_THREAD,
        "status": ExecutionStatus.QUEUED,
        "created_at": utc_now(),
        "project_epoch": 3,
    }
    values.update(overrides)
    return ExecutionRecord(**values)


def test_error_codes_are_exactly_the_prd_table() -> None:
    assert frozenset(PRD_ERROR_CODES) == ERROR_CODES


def test_framework_error_carries_its_code_and_message() -> None:
    error = FrameworkError(LAYER_NOT_FOUND, "Layer does not exist")
    assert error.code == LAYER_NOT_FOUND
    assert error.message == "Layer does not exist"
    assert str(error) == "Layer does not exist"


def test_framework_error_refuses_a_code_outside_the_table() -> None:
    with pytest.raises(ValueError):
        FrameworkError("NOT_A_CODE", "message")


def test_error_detail_refuses_a_code_outside_the_table() -> None:
    with pytest.raises(ValidationError):
        ErrorDetail(code="NOT_A_CODE", message="message")


def test_timestamps_are_iso_utc_with_an_offset() -> None:
    parsed = datetime.fromisoformat(utc_now())
    offset = parsed.utcoffset()
    assert offset is not None and offset.total_seconds() == 0


def test_succeeded_record_carries_the_result_and_is_complete() -> None:
    succeeded = record(
        status=ExecutionStatus.SUCCEEDED,
        result={"layers": []},
        progress=100,
        finished_at=utc_now(),
    )
    assert succeeded.model_dump(mode="json")["status"] == "SUCCEEDED"


@pytest.mark.parametrize(
    "overrides",
    [
        {"result": None},
        {"error": ErrorDetail(code=INTERNAL_ERROR, message="boom")},
        {"progress": 99},
        {"finished_at": None},
    ],
)
def test_succeeded_record_refuses_an_incomplete_state(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "status": ExecutionStatus.SUCCEEDED,
        "result": {"ok": True},
        "progress": 100,
        "finished_at": utc_now(),
    }
    values.update(overrides)
    with pytest.raises(ValidationError):
        record(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"error": None},
        {"result": {"ok": True}},
        {"finished_at": None},
    ],
)
def test_failed_record_refuses_an_incomplete_state(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "status": ExecutionStatus.FAILED,
        "error": ErrorDetail(code=INTERNAL_ERROR, message="boom"),
        "finished_at": utc_now(),
    }
    values.update(overrides)
    with pytest.raises(ValidationError):
        record(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"result": {"ok": True}},
        {"error": ErrorDetail(code=INTERNAL_ERROR, message="boom")},
        {"finished_at": None},
    ],
)
def test_cancelled_record_carries_neither_result_nor_error(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "status": ExecutionStatus.CANCELLED,
        "finished_at": utc_now(),
    }
    values.update(overrides)
    with pytest.raises(ValidationError):
        record(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"finished_at": utc_now()},
        {"result": {"ok": True}},
        {"error": ErrorDetail(code=INTERNAL_ERROR, message="boom")},
    ],
)
def test_non_terminal_record_has_no_outcome(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        record(**overrides)


@pytest.mark.parametrize("progress", [-1, 101])
def test_progress_stays_between_zero_and_hundred(progress: float) -> None:
    with pytest.raises(ValidationError):
        record(progress=progress)


def test_operation_spec_has_exactly_the_prd_fields() -> None:
    assert set(OperationSpec.model_fields) == {
        "name",
        "description",
        "kind",
        "strategy",
        "destructive",
        "input_schema",
        "output_schema",
        "pydantic_major",
    }


def test_cancel_receipt_has_the_prd_fields_and_no_reason_by_default() -> None:
    receipt = CancelReceipt(execution_id="exec-1", accepted=False, status=ExecutionStatus.QUEUED)
    assert receipt.reason is None
    assert set(CancelReceipt.model_fields) == {
        "execution_id",
        "accepted",
        "status",
        "reason",
    }


def test_unknown_input_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        StrictParams.model_validate({"unknown": 1})


def test_string_true_is_not_a_boolean() -> None:
    with pytest.raises(ValidationError):
        StrictParams.model_validate({"enabled": "true"})
