"""The registry: one name, one declaration, and specs that cannot be edited."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from qgis_agent_plugin.core import pydantic_compat
from qgis_agent_plugin.core.contracts import (
    BaseCommand,
    BaseOperation,
    BaseQuery,
    ExecutionStrategy,
    MainThreadOperationMixin,
    OperationContext,
    OperationKind,
)
from qgis_agent_plugin.core.models import (
    DUPLICATE_OPERATION,
    INVALID_OPERATION_DEFINITION,
    OPERATION_NOT_FOUND,
    FrameworkError,
    Params,
)
from qgis_agent_plugin.core.registry import OperationRegistry


class NoParams(Params):
    """An operation with no parameters still refuses an unknown key."""


class EmptyResult(BaseModel):
    """An operation with no result fields."""


class QueryOp(BaseQuery[NoParams, EmptyResult], MainThreadOperationMixin[NoParams, EmptyResult]):
    name = "layer.list"
    description = "list the layers of the current project"
    input_model = NoParams
    output_model = EmptyResult

    def execute_main(self, ctx: OperationContext, request: NoParams) -> EmptyResult:
        return EmptyResult()


class CommandOp(
    BaseCommand[NoParams, EmptyResult], MainThreadOperationMixin[NoParams, EmptyResult]
):
    name = "project.save"
    description = "save the current project"
    destructive = True
    input_model = NoParams
    output_model = EmptyResult

    def execute_main(self, ctx: OperationContext, request: NoParams) -> EmptyResult:
        return EmptyResult()


class StrategylessOp(BaseQuery[NoParams, EmptyResult]):
    name = "test.strategy"
    description = "declares no execution strategy"
    input_model = NoParams
    output_model = EmptyResult


def registry_with(*operations: BaseOperation) -> OperationRegistry:
    registry = OperationRegistry()
    for operation in operations:
        registry.register(operation)
    return registry


def test_a_registered_operation_is_returned_by_its_name() -> None:
    operation = QueryOp()
    assert registry_with(operation).get("layer.list") is operation


def test_duplicate_registration_is_refused_and_keeps_the_first() -> None:
    first = QueryOp()
    registry = registry_with(first)
    second = QueryOp()
    second.description = "a different description"
    with pytest.raises(FrameworkError) as caught:
        registry.register(second)
    assert caught.value.code == DUPLICATE_OPERATION
    assert registry.get("layer.list") is first
    assert registry.get_spec("layer.list").description == first.description


@pytest.mark.parametrize(
    "name",
    ["Layer.list", "layer", "layer.list.more", "layer..list", "layer.9list", ""],
)
def test_an_invalid_name_is_refused(name: str) -> None:
    operation = QueryOp()
    operation.name = name
    with pytest.raises(FrameworkError) as caught:
        OperationRegistry().register(operation)
    assert caught.value.code == INVALID_OPERATION_DEFINITION


def test_a_declaration_the_protocol_refuses_is_refused() -> None:
    with pytest.raises(FrameworkError) as caught:
        OperationRegistry().register(StrategylessOp())
    assert caught.value.code == INVALID_OPERATION_DEFINITION


def test_list_specs_is_ordered_by_name() -> None:
    registry = registry_with(CommandOp(), QueryOp())
    assert [spec.name for spec in registry.list_specs()] == ["layer.list", "project.save"]


def test_a_spec_carries_the_declaration_and_the_schemas() -> None:
    spec = registry_with(CommandOp()).get_spec("project.save")
    assert spec.kind is OperationKind.COMMAND
    assert spec.strategy is ExecutionStrategy.MAIN_THREAD
    assert spec.destructive is True
    assert spec.pydantic_major == pydantic_compat.major()
    assert spec.input_schema["additionalProperties"] is False
    assert spec.output_schema["properties"] == {}


def test_a_spec_is_a_deep_copy_of_the_registry_entry() -> None:
    registry = registry_with(QueryOp())
    spec = registry.get_spec("layer.list")
    spec.input_schema["title"] = "edited"
    spec.output_schema["properties"]["bogus"] = {}
    fresh = registry.get_spec("layer.list")
    assert fresh.input_schema["title"] != "edited"
    assert "bogus" not in fresh.output_schema["properties"]


def test_list_specs_returns_copies_too() -> None:
    registry = registry_with(QueryOp())
    specs = registry.list_specs()
    specs[0].input_schema["title"] = "edited"
    assert registry.list_specs()[0].input_schema["title"] != "edited"


def test_an_unknown_name_is_refused() -> None:
    registry = registry_with(QueryOp())
    for lookup in (registry.get, registry.get_spec):
        with pytest.raises(FrameworkError) as caught:
            lookup("layer.missing")
        assert caught.value.code == OPERATION_NOT_FOUND
