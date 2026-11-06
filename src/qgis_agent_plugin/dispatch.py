"""The pipeline every command goes through.

Look the command up in the surface, check the execution mode it asked for,
validate its params, run it -- ``run`` in this thread, ``arun`` elsewhere -- and
hand its result out as an answer.  A failure answers with
``{"status": "error", "code": ..., "message": ...}``: the code is the stable half
a caller branches on, taken from an explained ``CommandError`` and
``internal_error`` when the failure was not expected.

The pipeline imports the contract, not the commands: the command table and the
context source are injected, so this module loads without QGIS, PyQt or a socket.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from .command import Command, CommandContext, Result
from .protocol import CommandEnvelope, CommandError, failure, success
from .validation import describe

logger = logging.getLogger(__name__)

# What a bound pipeline is: one callable the socket and the debug page both take.
Handle = Callable[[Any], dict[str, Any]]


def dispatcher(context: Callable[[], CommandContext], commands: Mapping[str, Command]) -> Handle:
    """Bind the pipeline to one context source and one command table."""

    def handle(command: Any) -> dict[str, Any]:
        """Run one command and answer with a success or error envelope."""

        try:
            envelope = CommandEnvelope.model_validate(command)
        except ValidationError as error:
            # A command that is not an object at all is not a field failure.
            if any(not item["loc"] and item["type"] == "model_type" for item in error.errors()):
                return failure("invalid_command", "A command must be a JSON object")
            return failure("invalid_command", describe(error))

        name = envelope.type
        action = commands.get(name)
        if action is None:
            return failure("unknown_command", f"Unknown command type: {name}")
        # The requested mode is checked with the command name, before the params:
        # asking a synchronous command for async execution has to fail for that
        # reason, not for a missing field.  The modes are the ones the declaration
        # offers, default-first, so a "both" command stays synchronous unless it
        # is asked otherwise.
        execution = envelope.execution or action.modes[0]
        if execution not in action.modes:
            return failure(
                "unsupported_execution",
                f"{name} does not support {execution!r} execution",
            )

        try:
            params = action.params_model.model_validate(envelope.params or {})
        except ValidationError as error:
            return failure("invalid_field", describe(error))

        try:
            run = action.run if execution == "sync" else action.arun
            result: Result = run(context(), params)
            # Dumping is inside the try too: a command returning something that is
            # not a Result is a defect here, and has to answer as one.
            return success(result)
        except CommandError as error:
            # An explained failure: the code it was raised with is what leaves.
            logger.warning("%s: %s", error.code, error)
            return failure(error.code, str(error))
        except Exception as error:
            # Anything else is a defect here and is worth the stack.
            logger.exception("QGIS Agent command failed")
            return failure("internal_error", str(error))

    return handle
