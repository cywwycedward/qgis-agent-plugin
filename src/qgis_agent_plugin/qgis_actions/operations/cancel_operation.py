"""Cancel one asynchronous operation."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ..base import Action, ActionContext, BaseParams


class CancelOperationParams(BaseParams):
    """The operation to cancel."""

    operation_id: str = Field(min_length=1, max_length=128)


class CancelOperation(Action):
    """Ask the operation manager to cancel ``operation_id``."""

    name = "cancel_operation"
    read_only = False
    params_model = CancelOperationParams

    def run(self, ctx: ActionContext, params: CancelOperationParams) -> dict[str, Any]:
        return ctx.operations.cancel(params.operation_id)
