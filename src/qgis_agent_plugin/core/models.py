"""What a caller receives, and the error contract the framework speaks.

Every field is a JSON-safe value: an execution record never holds a QGIS object,
so it can be handed out, logged and compared as it is.  The record also refuses
to be built in a state the state machine cannot reach -- an impossible
combination is an engine defect, and repairing it silently would hide it.

``OperationKind`` and ``ExecutionStrategy`` live here rather than in
``contracts`` because a record declares them as fields while the protocol only
refers to them; defining them in ``contracts`` would make the two modules import
each other.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

# The whole error vocabulary, from PRD 11.2: one more or one less is a defect.
OPERATION_NOT_FOUND = "OPERATION_NOT_FOUND"
INVALID_OPERATION_DEFINITION = "INVALID_OPERATION_DEFINITION"
DUPLICATE_OPERATION = "DUPLICATE_OPERATION"
WRONG_THREAD = "WRONG_THREAD"
ENGINE_CLOSING = "ENGINE_CLOSING"
UNSUPPORTED_EXECUTION_MODE = "UNSUPPORTED_EXECUTION_MODE"
EXECUTION_NOT_FOUND = "EXECUTION_NOT_FOUND"
INVALID_INPUT = "INVALID_INPUT"
INVALID_CRS = "INVALID_CRS"
PROJECT_REPLACE_REQUIRED = "PROJECT_REPLACE_REQUIRED"
PROJECT_OPEN_FAILED = "PROJECT_OPEN_FAILED"
PROJECT_SAVE_FAILED = "PROJECT_SAVE_FAILED"
PROJECT_PATH_REQUIRED = "PROJECT_PATH_REQUIRED"
TARGET_EXISTS = "TARGET_EXISTS"
PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
INVALID_LAYER = "INVALID_LAYER"
LAYER_NOT_FOUND = "LAYER_NOT_FOUND"
LAYER_NOT_IN_TREE = "LAYER_NOT_IN_TREE"
INVALID_FIELD_TYPE = "INVALID_FIELD_TYPE"
SNAPSHOT_LIMIT_EXCEEDED = "SNAPSHOT_LIMIT_EXCEEDED"
TOO_MANY_EXECUTIONS = "TOO_MANY_EXECUTIONS"
TASK_SUBMISSION_FAILED = "TASK_SUBMISSION_FAILED"
STALE_PROJECT = "STALE_PROJECT"
OUTPUT_VALIDATION_FAILED = "OUTPUT_VALIDATION_FAILED"
INTERNAL_ERROR = "INTERNAL_ERROR"

ERROR_CODES = frozenset(
    {
        OPERATION_NOT_FOUND,
        INVALID_OPERATION_DEFINITION,
        DUPLICATE_OPERATION,
        WRONG_THREAD,
        ENGINE_CLOSING,
        UNSUPPORTED_EXECUTION_MODE,
        EXECUTION_NOT_FOUND,
        INVALID_INPUT,
        INVALID_CRS,
        PROJECT_REPLACE_REQUIRED,
        PROJECT_OPEN_FAILED,
        PROJECT_SAVE_FAILED,
        PROJECT_PATH_REQUIRED,
        TARGET_EXISTS,
        PATH_NOT_ALLOWED,
        INVALID_LAYER,
        LAYER_NOT_FOUND,
        LAYER_NOT_IN_TREE,
        INVALID_FIELD_TYPE,
        SNAPSHOT_LIMIT_EXCEEDED,
        TOO_MANY_EXECUTIONS,
        TASK_SUBMISSION_FAILED,
        STALE_PROJECT,
        OUTPUT_VALIDATION_FAILED,
        INTERNAL_ERROR,
    }
)


class OperationKind(StrEnum):
    """The business intent of an operation, not the thread it runs on."""

    COMMAND = "command"
    QUERY = "query"


class ExecutionStrategy(StrEnum):
    """Where the code runs, not whether the caller gets its result asynchronously."""

    MAIN_THREAD = "main_thread"
    BACKGROUND = "background"


class ExecutionStatus(StrEnum):
    """Every state a call can be in (PRD 8.1); the last three are terminal."""

    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    FINALIZING = "FINALIZING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


def utc_now() -> str:
    """The timestamp format every record field takes: ISO 8601, UTC, with offset."""

    return datetime.now(UTC).isoformat()


def _require_known_code(code: str) -> None:
    if code not in ERROR_CODES:
        raise ValueError(f"unknown error code: {code!r}")


class FrameworkError(Exception):
    """A control-plane failure: the call never became an execution.

    An invalid call -- unknown operation, unknown id, wrong thread, closed engine
    -- raises this instead of producing a record, which is how the caller's own
    mistake stays apart from an execution that failed (PRD 6.5).
    """

    def __init__(self, code: str, message: str) -> None:
        _require_known_code(code)
        super().__init__(message)
        self.code = code
        self.message = message


class ErrorDetail(BaseModel):
    """A failure a caller can branch on: a stable code, a short message, safe details."""

    model_config = ConfigDict(extra="forbid")

    code: StrictStr
    message: StrictStr
    details: dict[str, JsonValue] | None = None

    @field_validator("code")
    @classmethod
    def _code_is_known(cls, code: str) -> str:
        _require_known_code(code)
        return code


class CancelReceipt(BaseModel):
    """The answer to a cancel request; ``accepted`` never means already cancelled."""

    model_config = ConfigDict(extra="forbid")

    execution_id: StrictStr
    accepted: StrictBool
    status: ExecutionStatus
    reason: Literal["ALREADY_TERMINAL", "NOT_INTERRUPTIBLE", "FINALIZING"] | None = None


class ExecutionRecord(BaseModel):
    """One call's state, as last written by the main thread (PRD 6.6)."""

    model_config = ConfigDict(extra="forbid")

    execution_id: StrictStr
    operation: StrictStr
    kind: OperationKind
    strategy: ExecutionStrategy
    status: ExecutionStatus
    progress: float = Field(default=0, ge=0, le=100)
    created_at: StrictStr
    started_at: StrictStr | None = None
    finished_at: StrictStr | None = None
    project_epoch: int
    result: dict[str, Any] | None = None
    error: ErrorDetail | None = None

    @model_validator(mode="after")
    def _state_is_reachable(self) -> ExecutionRecord:
        status = self.status
        if status is ExecutionStatus.SUCCEEDED:
            self._require(self.result is not None, "a succeeded record carries a result")
            self._require(self.progress == 100, "a succeeded record is 100 complete")
            self._require(self.finished_at is not None, "a succeeded record has finished")
        elif status is ExecutionStatus.FAILED:
            self._require(self.result is None, "a failed record carries no result")
            self._require(self.error is not None, "a failed record carries an error")
            self._require(self.finished_at is not None, "a failed record has finished")
        elif status is ExecutionStatus.CANCELLED:
            self._require(self.result is None, "a cancelled record carries no result")
            self._require(self.error is None, "a cancelled record carries no error")
            self._require(self.finished_at is not None, "a cancelled record has finished")
        else:
            self._require(self.finished_at is None, "a non-terminal record has not finished")
            self._require(self.result is None, "a non-terminal record carries no result")
        if status is not ExecutionStatus.FAILED:
            self._require(self.error is None, "only a failed record carries an error")
        return self

    @staticmethod
    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)


class OperationSpec(BaseModel):
    """What a caller may ask about an operation without touching its code (PRD 6.3)."""

    model_config = ConfigDict(extra="forbid")

    name: StrictStr
    description: StrictStr
    kind: OperationKind
    strategy: ExecutionStrategy
    destructive: StrictBool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class Params(BaseModel):
    """Base for every operation input: an undeclared key is a caller mistake.

    Every input model inherits this so the rule cannot drift between operations,
    and an operation with no parameters gets an empty model that rejects
    anything.  Fields opt into ``StrictBool``/``StrictStr`` themselves, so a
    string ``"true"`` cannot pass for a boolean (PRD 6.1).  The operation this
    model is handed to is a caller that may have built the model itself, so an
    instance is revalidated instead of being taken on trust.
    """

    model_config = ConfigDict(extra="forbid", revalidate_instances="always")
