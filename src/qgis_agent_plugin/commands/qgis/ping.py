"""Connectivity check; no parameters and no QGIS state."""

from __future__ import annotations

from ...command import Command, CommandContext, Params, Result


class PingResult(Result):
    """The liveness answer: the server is up and running this command."""

    pong: bool = True


class Ping(Command):
    """Answer the MCP server's liveness probe."""

    name = "ping"

    def run(self, ctx: CommandContext, params: Params) -> PingResult:
        return PingResult()
