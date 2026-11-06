"""Report one operation started by an asynchronous command."""

from __future__ import annotations

from pydantic import Field

from ...command import Command, CommandContext, Params
from ...operations import Operation


class GetOperationParams(Params):
    """The operation to report."""

    operation_id: str = Field(min_length=1, max_length=128)


class GetOperation(Command):
    """Return the store's record of ``operation_id`, with its result once it succeeded.

    An unknown identifier is the store's own error and reaches the caller as an
    error envelope naming the id, so this command maps no errors of its own.
    """

    name = "get_operation"
    params_model = GetOperationParams

    def run(self, ctx: CommandContext, params: GetOperationParams) -> Operation:
        return ctx.operations.status(params.operation_id)
