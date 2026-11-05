"""Command dispatch for the qgis-agent-plugin.

``QGISSocketBridge`` owns the socket; this class owns the pipeline every
command goes through: look the command up in the registry, pick the execution
mode it asked for, validate its params, run the action -- ``run`` for a
synchronous command, ``arun`` for one that registers an operation -- and hand
its result out unchanged.  A failure answers with
``{"status": "error", "code": ..., "message": ...}``: the code is the stable
half a caller branches on, taken from the exception's own ``code`` when it
declares one and ``internal_error`` when the failure was not expected.

The gateway imports nothing from the action layer: the registry and the
context factory are injected, which keeps this module loadable without QGIS,
PyQt or a socket.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

logger = logging.getLogger(__name__)


def _error(code: str, message: str) -> dict[str, str]:
    """The one shape a failure leaves as: the stable code and its message."""

    return {"status": "error", "code": code, "message": message}


class CommandEnvelope(BaseModel):
    """The outer wire shape, before a registered action sees its parameters."""

    model_config = ConfigDict(extra="forbid")

    type: StrictStr = Field(min_length=1)
    params: dict[str, Any] | None = None
    execution: StrictStr | None = None


def _describe_envelope_error(error: ValidationError) -> str:
    """Render envelope failures without exposing Pydantic's internal URL."""

    lines = []
    for item in error.errors(include_url=False, include_input=False):
        if not item["loc"] and item["type"] == "model_type":
            return "A command must be a JSON object"
        field = ".".join(str(part) for part in item["loc"])
        message = item["msg"]
        lines.append(f"{field}: {message}" if field else message)
    return "\n".join(lines)


class QGISGateway:
    """Dispatch one decoded command through one fixed pipeline."""

    def __init__(self, context: Callable[[], Any], registry: Mapping[str, Any]) -> None:
        self.context = context  # () -> ActionContext, built per command
        self.registry = registry  # {command name: Action}

    def handle(self, command: Any) -> dict[str, Any]:
        """Run one command and answer with a success or error envelope."""

        try:
            envelope = CommandEnvelope.model_validate(command)
        except ValidationError as error:
            return _error("invalid_command", _describe_envelope_error(error))

        name = envelope.type
        action = self.registry.get(name)
        if action is None:
            return _error("unknown_command", f"Unknown command type: {name}")
        # The requested mode is checked with the command name, before the
        # params: asking a synchronous command for async execution has to fail
        # for that reason, not for a missing field.  The modes are the ones the
        # declaration offers, default-first, so a "both" command stays
        # synchronous unless it is asked otherwise.
        modes = getattr(action, "modes", ())
        execution = envelope.execution
        if execution is None:
            execution = modes[0] if modes else None
        if execution not in modes:
            return _error(
                "unsupported_execution",
                f"{name} does not support {execution!r} execution",
            )
        try:
            params = action.validate(envelope.params or {})
            ctx = self.context()
            run = action.run if execution == "sync" else action.arun
            result = run(ctx, params)
            return {"status": "success", "result": result}
        except Exception as error:
            # An error that names its own code is one the command expected, so the
            # failure is reported without a traceback; anything else is a defect
            # here and is worth the stack.
            code = getattr(error, "code", None)
            if code is None:
                logger.exception("QGIS Agent command failed")
                code = "internal_error"
            else:
                logger.warning("%s: %s", code, error)
            return _error(code, str(error))
