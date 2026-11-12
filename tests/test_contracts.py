"""The execution protocol: one strategy per operation, and the stages it needs."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest
from pydantic import BaseModel

from qgis_agent_plugin.core.config import Settings
from qgis_agent_plugin.core.contracts import (
    BackgroundOperationMixin,
    BaseCommand,
    BaseOperation,
    BaseQuery,
    ExecutionStrategy,
    MainThreadOperationMixin,
    OperationContext,
    OperationKind,
    TaskControl,
    validate_operation,
)
from qgis_agent_plugin.core.models import (
    INVALID_OPERATION_DEFINITION,
    FrameworkError,
    Params,
)


class LooseParams(BaseModel):
    """A plain model: it does not reject an undeclared key, so no operation may take one."""


class NoParams(Params):
    """An operation with no parameters still refuses an unknown key."""


class EmptyResult(BaseModel):
    """An operation with no result fields."""


class MainQuery(BaseQuery[NoParams, EmptyResult], MainThreadOperationMixin[NoParams, EmptyResult]):
    name = "layer.list"
    description = "list the layers of the current project"
    input_model = NoParams
    output_model = EmptyResult

    def execute_main(self, ctx: OperationContext, request: NoParams) -> EmptyResult:
        return EmptyResult()


class MainCommand(
    BaseCommand[NoParams, EmptyResult], MainThreadOperationMixin[NoParams, EmptyResult]
):
    name = "project.save"
    description = "save the current project"
    destructive = True
    input_model = NoParams
    output_model = EmptyResult

    def execute_main(self, ctx: OperationContext, request: NoParams) -> EmptyResult:
        return EmptyResult()


class WorkerQuery(
    BaseQuery[NoParams, EmptyResult],
    BackgroundOperationMixin[NoParams, EmptyResult, tuple[int, ...], int],
):
    name = "layer.sum_numeric_field"
    description = "sum a numeric field off the main thread"
    input_model = NoParams
    output_model = EmptyResult

    def prepare_main(self, ctx: OperationContext, request: NoParams) -> tuple[int, ...]:
        return ()

    def execute_worker(self, snapshot: tuple[int, ...], control: TaskControl) -> int:
        return sum(snapshot)

    def finalize_main(self, ctx: OperationContext, request: NoParams, result: int) -> EmptyResult:
        return EmptyResult()


class NamelessQuery(
    BaseQuery[NoParams, EmptyResult], MainThreadOperationMixin[NoParams, EmptyResult]
):
    description = "a query without a name"
    input_model = NoParams
    output_model = EmptyResult

    def execute_main(self, ctx: OperationContext, request: NoParams) -> EmptyResult:
        return EmptyResult()


class StrategylessQuery(BaseQuery[NoParams, EmptyResult]):
    name = "test.strategy"
    description = "declares no execution strategy"
    input_model = NoParams
    output_model = EmptyResult


class LazyMainQuery(BaseQuery[NoParams, EmptyResult]):
    name = "test.lazy"
    description = "declares main_thread but implements nothing"
    strategy = ExecutionStrategy.MAIN_THREAD
    input_model = NoParams
    output_model = EmptyResult


class UnpreparedBackground(BaseQuery[NoParams, EmptyResult]):
    name = "test.unprepared"
    description = "declares background but implements no stage"
    strategy = ExecutionStrategy.BACKGROUND
    input_model = NoParams
    output_model = EmptyResult


class BothStrategies(BaseQuery[NoParams, EmptyResult]):
    name = "test.both"
    description = "declares both execution strategies"
    strategy = ExecutionStrategy.MAIN_THREAD
    input_model = NoParams
    output_model = EmptyResult

    def execute_main(self, ctx: OperationContext, request: NoParams) -> EmptyResult:
        return EmptyResult()

    def prepare_main(self, ctx: OperationContext, request: NoParams) -> tuple[int, ...]:
        return ()

    def execute_worker(self, snapshot: tuple[int, ...], control: TaskControl) -> int:
        return 0

    def finalize_main(self, ctx: OperationContext, request: NoParams, result: int) -> EmptyResult:
        return EmptyResult()


class BareOperation(
    BaseOperation[NoParams, EmptyResult], MainThreadOperationMixin[NoParams, EmptyResult]
):
    name = "test.bare"
    description = "neither a command nor a query"
    input_model = NoParams
    output_model = EmptyResult

    def execute_main(self, ctx: OperationContext, request: NoParams) -> EmptyResult:
        return EmptyResult()


def refusal(operation: object) -> FrameworkError:
    with pytest.raises(FrameworkError) as caught:
        validate_operation(operation)
    assert caught.value.code == INVALID_OPERATION_DEFINITION
    return caught.value


def test_enum_values_follow_the_prd() -> None:
    assert OperationKind.COMMAND == "command"
    assert OperationKind.QUERY == "query"
    assert ExecutionStrategy.MAIN_THREAD == "main_thread"
    assert ExecutionStrategy.BACKGROUND == "background"


def test_command_and_query_fix_the_kind_and_destructive_defaults() -> None:
    query = MainQuery()
    command = MainCommand()
    assert query.kind is OperationKind.QUERY
    assert command.kind is OperationKind.COMMAND
    assert query.destructive is False
    assert command.destructive is True


def test_main_thread_mixin_fixes_the_strategy() -> None:
    assert MainThreadOperationMixin.strategy is ExecutionStrategy.MAIN_THREAD
    assert BackgroundOperationMixin.strategy is ExecutionStrategy.BACKGROUND


def test_a_main_thread_operation_declaration_is_accepted() -> None:
    validate_operation(MainQuery())
    validate_operation(MainCommand())


def test_a_background_operation_declaration_is_accepted() -> None:
    validate_operation(WorkerQuery())


def test_task_control_exposes_cancel_and_progress_only() -> None:
    assert {name for name in vars(TaskControl) if not name.startswith("_")} == {
        "is_cancelled",
        "set_progress",
    }


def test_operation_context_carries_the_main_thread_handles() -> None:
    policy = Settings()
    context = OperationContext(
        iface="iface", project="project", lifecycle="lifecycle", policy=policy
    )
    assert [field.name for field in fields(context)] == ["iface", "project", "lifecycle", "policy"]
    assert context.policy is policy
    with pytest.raises(FrozenInstanceError):
        context.iface = "other"  # type: ignore[misc]


def test_a_non_operation_is_refused() -> None:
    refusal(object())


def test_a_missing_name_is_refused() -> None:
    refusal(NamelessQuery())


def test_an_empty_description_is_refused() -> None:
    operation = MainQuery()
    operation.description = "  "
    refusal(operation)


def test_a_non_model_input_is_refused() -> None:
    operation = MainQuery()
    operation.input_model = dict  # type: ignore
    refusal(operation)


def test_an_input_model_that_is_not_params_is_refused() -> None:
    operation = MainQuery()
    operation.input_model = LooseParams  # type: ignore[assignment]
    assert "Params" in refusal(operation).message


def test_a_non_boolean_destructive_flag_is_refused() -> None:
    operation = MainQuery()
    operation.destructive = "yes"  # type: ignore
    refusal(operation)


def test_a_kind_that_contradicts_the_base_class_is_refused() -> None:
    operation = MainQuery()
    operation.kind = OperationKind.COMMAND
    refusal(operation)


def test_an_operation_that_is_neither_command_nor_query_is_refused() -> None:
    refusal(BareOperation())


def test_a_missing_strategy_is_refused() -> None:
    refusal(StrategylessQuery())


def test_main_thread_without_execute_main_is_refused() -> None:
    assert "execute_main" in refusal(LazyMainQuery()).message


def test_background_without_all_stages_is_refused() -> None:
    assert "prepare_main" in refusal(UnpreparedBackground()).message


def test_both_strategies_declared_is_refused() -> None:
    assert "both" in refusal(BothStrategies()).message
