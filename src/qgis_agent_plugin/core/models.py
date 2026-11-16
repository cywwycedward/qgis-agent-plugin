"""调用方拿到的东西，以及框架共用的错误契约。

每个字段都是 JSON 安全值：执行记录里永远不放 QGIS 对象，所以它可以直接外传、记日志、逐字段比较。
记录也不允许被构造成状态机到不了的状态——不可能的组合就是引擎的缺陷，悄悄补齐只会把它藏起来。

``OperationKind`` 与 ``ExecutionStrategy`` 放在这里而不是 ``contracts``：记录把它们声明成字段，
而协议只是引用它们；定义在 ``contracts`` 会让两个模块互相导入。

What a caller receives, and the error contract the framework speaks.

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

# 完整的错误词表：多一个或少一个都是缺陷。
# The whole error vocabulary: one more or one less is a defect.
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
        TOO_MANY_EXECUTIONS,
        TASK_SUBMISSION_FAILED,
        STALE_PROJECT,
        OUTPUT_VALIDATION_FAILED,
        INTERNAL_ERROR,
    }
)


class OperationKind(StrEnum):
    """操作的业务意图，而不是它跑在哪个线程。

    The business intent of an operation, not the thread it runs on.
    """

    COMMAND = "command"
    QUERY = "query"


class ExecutionStrategy(StrEnum):
    """代码在哪里跑，而不是调用方是否异步拿到结果。

    Where the code runs, not whether the caller gets its result asynchronously.
    """

    MAIN_THREAD = "main_thread"
    BACKGROUND = "background"


class ExecutionStatus(StrEnum):
    """一次调用可能处的每个状态；最后三个是终态。

    Every state a call can be in; the last three are terminal.
    """

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
    """每条记录字段采用的时间戳格式：ISO 8601、UTC、带时区偏移。

    The timestamp format every record field takes: ISO 8601, UTC, with offset.
    """

    return datetime.now(UTC).isoformat()


def _require_known_code(code: str) -> None:
    if code not in ERROR_CODES:
        raise ValueError(f"unknown error code: {code!r}")


class FrameworkError(Exception):
    """控制面的失败：这次调用根本没有成为一次执行。

    一次不合法的调用——未知操作、未知 id、错线程、引擎已关闭——抛它而不是产生记录，调用方自己的
    错因此与「一次真正失败了的执行」区分得开。

    A control-plane failure: the call never became an execution.

    An invalid call -- unknown operation, unknown id, wrong thread, closed engine
    -- raises this instead of producing a record, which is how the caller's own
    mistake stays apart from an execution that failed.
    """

    def __init__(self, code: str, message: str) -> None:
        _require_known_code(code)
        super().__init__(message)
        self.code = code
        self.message = message


class ErrorDetail(BaseModel):
    """调用方可以据此分支的失败：稳定的错误码、短消息、安全的细节。

    A failure a caller can branch on: a stable code, a short message, safe details.
    """

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
    """取消请求的答复；``accepted`` 从不表示已经取消。

    The answer to a cancel request; ``accepted`` never means already cancelled.
    """

    model_config = ConfigDict(extra="forbid")

    execution_id: StrictStr
    accepted: StrictBool
    status: ExecutionStatus
    reason: Literal["ALREADY_TERMINAL", "NOT_INTERRUPTIBLE", "FINALIZING"] | None = None


class ExecutionRecord(BaseModel):
    """一次调用的状态，以主线程最后一次写入为准。

    One call's state, as last written by the main thread.
    """

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
    """调用方不碰操作代码就能问到的信息。

    What a caller may ask about an operation without touching its code.
    """

    model_config = ConfigDict(extra="forbid")

    name: StrictStr
    description: StrictStr
    kind: OperationKind
    strategy: ExecutionStrategy
    destructive: StrictBool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class Params(BaseModel):
    """每个操作输入的基类：没声明的键就是调用方的错。

    每个输入模型都继承它，规则就不会在操作之间漂移；没有参数的操作拿到一个空模型，它会拒绝任何东西。
    字段自己选用 ``StrictBool``/``StrictStr``，所以字符串 ``"true"`` 冒充不了布尔值。交给这个模型的
    操作自己也可能就是造模型的那个调用方，所以实例会重新校验，而不是被直接信任。

    Base for every operation input: an undeclared key is a caller mistake.

    Every input model inherits this so the rule cannot drift between operations,
    and an operation with no parameters gets an empty model that rejects
    anything.  Fields opt into ``StrictBool``/``StrictStr`` themselves, so a
    string ``"true"`` cannot pass for a boolean.  The operation this model is
    handed to is a caller that may have built the model itself, so an instance is
    revalidated instead of being taken on trust.
    """

    model_config = ConfigDict(extra="forbid", revalidate_instances="always")


class Result(BaseModel):
    """每个操作输出的基类：没声明的键就是插件缺陷。

    结果是产出它的操作构造的，调用方从不构造它，所以模型没声明的键意味着操作报错了——丢掉它等于
    交给调用方一个谁都没产出过的默认值。每个结果都继承它，规则就不会在操作之间漂移。

    Base for every operation output: an undeclared key is a plugin defect.

    A result is built by the operation that produced it, never by a caller, so a
    key the model does not declare means the operation reports something wrong --
    dropping it would hand the caller a default nobody produced.  Every result
    inherits this so the rule cannot drift between operations.
    """

    model_config = ConfigDict(extra="forbid")
