"""Connectivity check; no parameters and no QGIS state."""

from __future__ import annotations

from typing import Any

from ..base import Action, ActionContext, BaseParams


class Ping(Action):
    """Answer the MCP server's liveness probe."""

    name = "ping"

    def run(self, ctx: ActionContext, params: BaseParams) -> dict[str, Any]:
        return {"pong": True}
