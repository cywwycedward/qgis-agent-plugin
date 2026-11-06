"""Cancel one asynchronous operation."""

from __future__ import annotations

from pydantic import Field

from ...command import Command, CommandContext, Params
from ...operations import Operation


class CancelOperationParams(Params):
    """The operation to cancel."""

    operation_id: str = Field(min_length=1, max_length=128)


class CancelOperation(Command):
    """Ask the store to cancel ``operation_id``."""

    name = "cancel_operation"
    params_model = CancelOperationParams

    def run(self, ctx: CommandContext, params: CancelOperationParams) -> Operation:
        return ctx.operations.cancel(params.operation_id)
