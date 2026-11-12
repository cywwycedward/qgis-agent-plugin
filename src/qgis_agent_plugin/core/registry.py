"""按名字查操作的唯一地方。

注册就是关卡：它把声明读一次、把 spec 建一次，并把两者都私有保存，于是写坏的声明在启动时就失败而
不是在第一次调用时，也没有调用方能靠持有一个引用去改这份对外表面。这里不执行任何 PyQGIS 业务。

The one place an operation is looked up by name.

Registration is the gate: it reads a declaration once, builds its spec once, and
keeps both private, so a broken definition fails at start-up instead of at the
first call and no caller can edit the surface by holding a reference.  Nothing
here executes PyQGIS business.
"""

from __future__ import annotations

import re

from .contracts import BaseOperation, validate_operation
from .models import (
    DUPLICATE_OPERATION,
    INVALID_OPERATION_DEFINITION,
    OPERATION_NOT_FOUND,
    FrameworkError,
    OperationSpec,
)

# 注册键的样子：``<组>.<名字>``，例如 ``layer.add``。
# What a registration key looks like: ``<group>.<name>``, as in ``layer.add``.
_NAME = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")


class OperationRegistry:
    """这个插件提供的操作集合，在 ``initGui`` 跑过之后固定下来。

    The set of operations this plugin offers, fixed once ``initGui`` runs.
    """

    def __init__(self) -> None:
        self._operations: dict[str, BaseOperation] = {}
        self._specs: dict[str, OperationSpec] = {}

    def register(self, operation: BaseOperation) -> None:
        """加一个操作；声明不合法或名字已被占用都拒绝。

        Add one operation, refusing a bad declaration or a taken name.
        """

        name = getattr(operation, "name", None)
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise FrameworkError(INVALID_OPERATION_DEFINITION, f"invalid operation name: {name!r}")
        validate_operation(operation)
        if name in self._operations:
            raise FrameworkError(DUPLICATE_OPERATION, f"{name} is already registered")
        spec = _spec(operation)
        self._operations[name] = operation
        self._specs[name] = spec

    def get(self, name: str) -> BaseOperation:
        """``name`` 名下注册的那个操作。

        The operation registered under ``name``.
        """

        try:
            return self._operations[name]
        except KeyError:
            raise FrameworkError(OPERATION_NOT_FOUND, f"unknown operation: {name}") from None

    def get_spec(self, name: str) -> OperationSpec:
        """操作 spec 的副本，所以改它改不到注册表。

        A copy of the operation's spec, so editing it cannot edit the registry.
        """

        try:
            spec = self._specs[name]
        except KeyError:
            raise FrameworkError(OPERATION_NOT_FOUND, f"unknown operation: {name}") from None
        return spec.model_copy(deep=True)

    def list_specs(self) -> list[OperationSpec]:
        """全部 spec，按名字排序，好让这份表面稳定、可比较。

        Every spec, ordered by name so the surface is stable and comparable.
        """

        return [self.get_spec(name) for name in sorted(self._specs)]


def _spec(operation: BaseOperation) -> OperationSpec:
    return OperationSpec(
        name=operation.name,
        description=operation.description,
        kind=operation.kind,
        strategy=operation.strategy,
        destructive=operation.destructive,
        input_schema=operation.input_model.model_json_schema(),
        output_schema=operation.output_model.model_json_schema(),
    )
