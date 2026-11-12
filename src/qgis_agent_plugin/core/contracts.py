"""The execution protocol: what an operation declares and what it is handed.

An operation states its business intent (``kind``) and where its code may run
(``strategy``) once, as class attributes, and implements the stages that
strategy requires.  The mixins carry the strategy value and the stage methods;
``validate_operation`` is the single reader of those declarations, so a broken
one is refused at registration instead of at the first call.

Nothing here imports QGIS or Qt, at module level or inside a function: the whole
protocol has to load in a plain interpreter, which is what keeps the registry and
the schema export testable without a running QGIS.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol

from pydantic import BaseModel

from .config import Settings
from .models import (
    INVALID_OPERATION_DEFINITION,
    ExecutionStrategy,
    FrameworkError,
    OperationKind,
    Params,
)

__all__ = [
    "BaseCommand",
    "BaseOperation",
    "BaseQuery",
    "BackgroundOperationMixin",
    "ExecutionStrategy",
    "MainThreadOperationMixin",
    "OperationContext",
    "OperationKind",
    "TaskControl",
    "validate_operation",
]

# The three stages a background operation must implement, in execution order.
BACKGROUND_STAGES = ("prepare_main", "execute_worker", "finalize_main")


class TaskControl(Protocol):
    """Everything a worker thread may do to the execution that owns it.

    The 0-100 clamping of ``set_progress`` belongs to the adapter, not here: this
    is a protocol the worker is handed, and it grants no access to the project,
    the interface or the execution record.
    """

    def is_cancelled(self) -> bool: ...

    def set_progress(self, value: float) -> None: ...


@dataclass(frozen=True)
class OperationContext:
    """The main-thread handles an operation may use; never handed to a worker."""

    iface: Any
    project: Any
    lifecycle: Any
    policy: Settings


class BaseOperation[InputT: BaseModel, OutputT: BaseModel](ABC):
    """One named capability: its wire contract and where its code runs."""

    name: str
    description: str
    input_model: type[InputT]
    output_model: type[OutputT]
    kind: OperationKind
    strategy: ExecutionStrategy
    destructive: bool = False


class BaseCommand[InputT: BaseModel, OutputT: BaseModel](BaseOperation[InputT, OutputT]):
    """An operation that intends to change user-observable state."""

    kind = OperationKind.COMMAND


class BaseQuery[InputT: BaseModel, OutputT: BaseModel](BaseOperation[InputT, OutputT]):
    """An operation that only reads state; its side effects are caches and logs."""

    kind = OperationKind.QUERY


class MainThreadOperationMixin[InputT: BaseModel, OutputT: BaseModel](ABC):
    """An operation that runs on the QGIS main thread, synchronously."""

    strategy = ExecutionStrategy.MAIN_THREAD

    @abstractmethod
    def execute_main(self, ctx: OperationContext, request: InputT) -> OutputT: ...


class BackgroundOperationMixin[InputT: BaseModel, OutputT: BaseModel, SnapshotT, WorkerT](ABC):
    """An operation that prepares on the main thread and computes off it.

    ``prepare_main`` reads and copies, ``execute_worker`` touches no QGIS object,
    ``finalize_main`` validates and commits on the main thread.  The split is the
    contract that keeps a ``QgsProject`` out of a worker thread (PRD 6.2).
    """

    strategy = ExecutionStrategy.BACKGROUND

    @abstractmethod
    def prepare_main(self, ctx: OperationContext, request: InputT) -> SnapshotT: ...

    @abstractmethod
    def execute_worker(self, snapshot: SnapshotT, control: TaskControl) -> WorkerT: ...

    @abstractmethod
    def finalize_main(self, ctx: OperationContext, request: InputT, result: WorkerT) -> OutputT: ...


def validate_operation(operation: object) -> None:
    """Refuse a declaration the engine could not dispatch (PRD 6.2, 6.4).

    The registry calls this once per registration; every refusal is
    ``INVALID_OPERATION_DEFINITION`` and names what is missing, because the
    declaration is code, not caller input.
    """

    if not isinstance(operation, BaseOperation):
        raise FrameworkError(
            INVALID_OPERATION_DEFINITION, "an operation must inherit BaseOperation"
        )

    name = getattr(operation, "name", None)
    if not isinstance(name, str) or not name:
        raise FrameworkError(INVALID_OPERATION_DEFINITION, "an operation needs a name")

    def refuse(why: str) -> NoReturn:
        raise FrameworkError(INVALID_OPERATION_DEFINITION, f"{name}: {why}")

    description = getattr(operation, "description", None)
    if not isinstance(description, str) or not description.strip():
        refuse("description must be a non-empty string")

    for attribute in ("input_model", "output_model"):
        model = getattr(operation, attribute, None)
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            refuse(f"{attribute} must be a pydantic model")

    # PRD 6.1: an input model that is not ``Params`` may accept an undeclared key,
    # and the caller would then see it silently dropped instead of refused.
    if not issubclass(operation.input_model, Params):
        refuse("input_model must inherit Params")

    if not isinstance(operation.destructive, bool):
        refuse("destructive must be a boolean")

    is_command = isinstance(operation, BaseCommand)
    is_query = isinstance(operation, BaseQuery)
    if is_command == is_query:
        refuse("an operation is exactly one of BaseCommand or BaseQuery")
    expected = OperationKind.COMMAND if is_command else OperationKind.QUERY
    if operation.kind is not expected:
        refuse(f"kind must be {expected.value}")

    implements_main = callable(getattr(operation, "execute_main", None))
    implements_background = all(
        callable(getattr(operation, stage, None)) for stage in BACKGROUND_STAGES
    )
    strategy = getattr(operation, "strategy", None)
    if strategy is ExecutionStrategy.MAIN_THREAD:
        if not implements_main:
            refuse("main_thread requires execute_main")
        if implements_background:
            refuse("declares both execution strategies")
    elif strategy is ExecutionStrategy.BACKGROUND:
        if not implements_background:
            refuse("background requires " + ", ".join(BACKGROUND_STAGES))
        if implements_main:
            refuse("declares both execution strategies")
    else:
        refuse("strategy must be main_thread or background")
