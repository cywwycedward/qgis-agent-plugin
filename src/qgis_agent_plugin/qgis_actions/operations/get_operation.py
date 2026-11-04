"""Report one operation started by an asynchronous command."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ..base import Action, ActionContext, BaseParams


class GetOperationParams(BaseParams):
    """The operation to report."""

    operation_id: str = Field(min_length=1, max_length=128)


class GetOperation(Action):
    """Return the manager's snapshot of ``operation_id``, with its result once it succeeded.

    An unknown identifier is the manager's own error and reaches the caller as an
    error envelope naming the id, so this command maps no errors of its own.
    """

    name = "get_operation"
    params_model = GetOperationParams

    def run(self, ctx: ActionContext, params: GetOperationParams) -> dict[str, Any]:
        return ctx.operations.status(params.operation_id)
