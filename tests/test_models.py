"""执行记录、错误契约与输入模型必须遵守的规则；三者都是纯 JSON，可以原样交给调用方。

Execution records, the error contract and the rules every operation input obeys; all
three are plain JSON, so they can be handed to a caller as they are.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, StrictBool, ValidationError

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

# 错误码全集：多一个或少一个都是缺陷。
# 它是调用方唯一能分支的词汇表。
# The whole error vocabulary: one more is a defect.
# It is the only vocabulary the callers can branch on.
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
    "TOO_MANY_EXECUTIONS",
    "TASK_SUBMISSION_FAILED",
    "STALE_PROJECT",
    "OUTPUT_VALIDATION_FAILED",
    "INTERNAL_ERROR",
}


class StrictParams(Params):
    """一个合法的操作输入模型：两个键都可省，多给一个键仍然被拒绝，布尔只收真布尔。

    A legal operation input model: both keys are optional, an extra key is still refused,
    and only a real boolean passes for a boolean.
    """

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


def test_operation_spec_has_exactly_the_documented_fields() -> None:
    # 规格里没有 ``pydantic_major``：只支持 pydantic 2。
    # 别的大版本在导入时被拒，字段因此不存在。
    # The spec has no ``pydantic_major`` field.
    # Refused at import, so the field never appears.
    assert set(OperationSpec.model_fields) == {
        "name",
        "description",
        "kind",
        "strategy",
        "destructive",
        "input_schema",
        "output_schema",
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


def test_cancel_receipt_reason_is_the_prd_set() -> None:
    for reason in ("ALREADY_TERMINAL", "NOT_INTERRUPTIBLE", "FINALIZING"):
        receipt = CancelReceipt(
            execution_id="exec-1",
            accepted=False,
            status=ExecutionStatus.CANCELLED,
            reason=reason,
        )
        assert receipt.reason == reason
    # 拼错正是这条断言的意义。
    # 没有消费者会去分支第四种 reason。
    # A typo is the whole point.
    # No consumer branches on a fourth reason value.
    with pytest.raises(ValidationError):
        CancelReceipt(
            execution_id="exec-1",
            accepted=False,
            status=ExecutionStatus.CANCELLED,
            reason="NOT_INTERUPTIBLE",
        )


def test_error_details_must_be_json() -> None:
    with pytest.raises(ValidationError):
        ErrorDetail(code=INTERNAL_ERROR, message="boom", details={"path": Path("x")})
    detail = ErrorDetail(code=INTERNAL_ERROR, message="boom", details={"field": "value"})
    assert detail.model_dump(mode="json")["details"] == {"field": "value"}


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (ErrorDetail, {"code": INTERNAL_ERROR, "message": "boom"}),
        (
            CancelReceipt,
            {"execution_id": "exec-1", "accepted": False, "status": ExecutionStatus.QUEUED},
        ),
        (
            ExecutionRecord,
            {
                "execution_id": "0f8fad5b-d9cb-469f-a165-70867728950e",
                "operation": "layer.list",
                "kind": OperationKind.QUERY,
                "strategy": ExecutionStrategy.MAIN_THREAD,
                "status": ExecutionStatus.QUEUED,
                "created_at": utc_now(),
                "project_epoch": 3,
            },
        ),
        (
            OperationSpec,
            {
                "name": "layer.list",
                "description": "list the layers of the current project",
                "kind": OperationKind.QUERY,
                "strategy": ExecutionStrategy.MAIN_THREAD,
                "destructive": False,
                "input_schema": {},
                "output_schema": {},
            },
        ),
    ],
)
def test_a_non_input_dto_refuses_an_undeclared_field(
    model: type[BaseModel], values: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        model(**values, surprise=1)


def test_unknown_input_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        StrictParams.model_validate({"unknown": 1})


def test_string_true_is_not_a_boolean() -> None:
    with pytest.raises(ValidationError):
        StrictParams.model_validate({"enabled": "true"})
