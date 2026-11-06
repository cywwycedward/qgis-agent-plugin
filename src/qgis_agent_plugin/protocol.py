"""The shapes that cross the wire, and the one failure the pipeline explains.

An answer leaves as ``{"status": ...}``; a failure adds the stable ``code`` a
caller branches on and a message a reader can act on.  ``success`` is the only
place a result becomes a wire value, so every command's answer is dumped one way
and a value pydantic cannot serialize fails where the field is, not at the socket.

``CommandError`` is the failure this plugin explains instead of crashing on: a
command raises it by code, the pipeline turns it into an answer, and a background
job carries it out of a thread that must not raise.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from .command import Result


class CommandError(Exception):
    """An expected failure carrying a stable code for callers and logs."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CommandEnvelope(BaseModel):
    """The outer wire shape, before a command sees its parameters."""

    model_config = ConfigDict(extra="forbid")

    type: StrictStr = Field(min_length=1)
    params: dict[str, Any] | None = None
    execution: StrictStr | None = None


def success(result: Result) -> dict[str, Any]:
    """One command's answer, with its result made JSON-safe."""

    return {"status": "success", "result": result.model_dump(mode="json")}


def failure(code: str, message: str) -> dict[str, str]:
    """One expected failure: the stable code and a message that reads on its own."""

    return {"status": "error", "code": code, "message": message}
