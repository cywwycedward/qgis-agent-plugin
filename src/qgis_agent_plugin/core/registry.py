"""The one place an operation is looked up by name.

Registration is the gate: it reads a declaration once, builds its spec once, and
keeps both private, so a broken definition fails at start-up instead of at the
first call and no caller can edit the surface by holding a reference.  Nothing
here executes PyQGIS business.
"""

from __future__ import annotations

import re

from . import pydantic_compat
from .contracts import BaseOperation, validate_operation
from .models import (
    DUPLICATE_OPERATION,
    INVALID_OPERATION_DEFINITION,
    OPERATION_NOT_FOUND,
    FrameworkError,
    OperationSpec,
)

# What a registration key looks like: ``<group>.<name>``, as in ``layer.add``.
_NAME = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")


class OperationRegistry:
    """The set of operations this plugin offers, fixed once ``initGui`` runs."""

    def __init__(self) -> None:
        self._operations: dict[str, BaseOperation] = {}
        self._specs: dict[str, OperationSpec] = {}

    def register(self, operation: BaseOperation) -> None:
        """Add one operation, refusing a bad declaration or a taken name."""

        name = getattr(operation, "name", None)
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise FrameworkError(
                INVALID_OPERATION_DEFINITION, f"invalid operation name: {name!r}"
            )
        validate_operation(operation)
        if name in self._operations:
            raise FrameworkError(DUPLICATE_OPERATION, f"{name} is already registered")
        spec = _spec(operation)
        self._operations[name] = operation
        self._specs[name] = spec

    def get(self, name: str) -> BaseOperation:
        """The operation registered under ``name``."""

        try:
            return self._operations[name]
        except KeyError:
            raise FrameworkError(OPERATION_NOT_FOUND, f"unknown operation: {name}") from None

    def get_spec(self, name: str) -> OperationSpec:
        """A copy of the operation's spec, so editing it cannot edit the registry."""

        try:
            spec = self._specs[name]
        except KeyError:
            raise FrameworkError(OPERATION_NOT_FOUND, f"unknown operation: {name}") from None
        return spec.model_copy(deep=True)

    def list_specs(self) -> list[OperationSpec]:
        """Every spec, ordered by name so the surface is stable and comparable."""

        return [self.get_spec(name) for name in sorted(self._specs)]


def _spec(operation: BaseOperation) -> OperationSpec:
    return OperationSpec(
        name=operation.name,
        description=operation.description,
        kind=operation.kind,
        strategy=operation.strategy,
        destructive=operation.destructive,
        input_schema=pydantic_compat.json_schema(operation.input_model),
        output_schema=pydantic_compat.json_schema(operation.output_model),
        pydantic_major=pydantic_compat.major(),
    )
