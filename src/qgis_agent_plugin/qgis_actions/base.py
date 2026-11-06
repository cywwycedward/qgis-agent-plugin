"""The action contract: one class per wire command.

A command class declares its wire ``name``, its ``params_model`` and its
``execution`` mode once, then implements ``run``, ``arun`` or both.  Everything
that is identical for every command -- parameter validation and the error
envelope -- belongs to the gateway pipeline, so the pipeline never needs to
know a specific command, and a command's result leaves that pipeline as ``run``
or ``arun`` returned it.

The declarations the pipeline does not consume yet (approval, capability
gating, deadlines) are added to this base class when those stages land, so each
new stage keeps costing one declaration plus one pipeline implementation instead
of one edit per command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from ..validation import describe

# The wire modes each execution offers, default-first: an omitted request takes
# the first one, so a "both" command stays synchronous unless it is asked
# otherwise.  The pipeline reads the modes off the declaration, so this is the
# one place the vocabulary lives.
_MODES = {"sync": ("sync",), "async": ("async",), "both": ("sync", "async")}
# The method each wire mode calls; a declaration has to implement the ones it offers.
_METHODS = {"sync": "run", "async": "arun"}
EXECUTIONS = tuple(_MODES)
_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


class ActionError(Exception):
    """An expected command failure carrying a stable code for callers and logs."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ActionContext:
    """The runtime handles one command may use; actions never reach globals."""

    iface: Any = None
    project: Any = None
    operations: Any = None  # the OperationRegistry, for asynchronous commands


class BaseParams(BaseModel):
    """Base for one command's parameters: an unknown key is a caller mistake.

    Every command's model inherits this, so the rule cannot drift between
    commands, and a command with no parameters of its own gets an empty model
    that rejects anything.  A model holds params only: how a failure of one
    rule reads is decided where that rule is written, and the pipeline renders
    the collected failures once for every command.
    """

    model_config = ConfigDict(extra="forbid")


class BaseResult(BaseModel):
    """Base for one command's result: the shape its ``run`` hands the pipeline.

    Every command's result model inherits this, so the rule that a key the
    model does not declare is a mistake holds for what a command builds as well
    as for what it is called with.  A command dumps its own result, because the
    pipeline hands the value out unchanged and the socket needs text-safe JSON.
    """

    model_config = ConfigDict(extra="forbid")


def _is_params_model(value: Any) -> bool:
    """True when a command declared a model that carries the shared param rules."""

    return isinstance(value, type) and issubclass(value, BaseParams)


class Action:
    """One wire command, declared and implemented in one place."""

    name: ClassVar[str]
    params_model: ClassVar[type[BaseParams]] = BaseParams
    execution: ClassVar[str] = "sync"  # sync | async | both
    modes: ClassVar[tuple[str, ...]] = ("sync",)  # the wire modes execution offers

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Reject a malformed declaration while the registry is built."""

        super().__init_subclass__(**kwargs)
        name = getattr(cls, "name", None)
        if not isinstance(name, str) or not _NAME.match(name):
            raise TypeError(f"invalid action name: {name!r}")
        if not _is_params_model(cls.params_model):
            raise TypeError(f"{name}: params_model must be a pydantic model")
        if cls.execution not in EXECUTIONS:
            raise TypeError(f"{name}: unsupported execution {cls.execution!r}")
        cls.modes = _MODES[cls.execution]
        for method in (_METHODS[mode] for mode in cls.modes):
            if getattr(cls, method, None) is getattr(Action, method, None):
                raise TypeError(f"{name}: execution {cls.execution!r} requires {method}()")

    def validate(self, raw: Any) -> BaseParams:
        """Normalize untrusted params into the model ``run`` receives.

        The rules live in ``params_model`` -- including any cross-field rule --
        so a command never overrides this method, and every command's failures
        read the same way.  A command is handed the model, not its dump: the
        types and the defaults it declared are still there.
        """

        try:
            return self.params_model.model_validate(raw)
        except ValidationError as error:
            raise ActionError("invalid_field", describe(error)) from error

    def run(self, ctx: ActionContext, params: Any) -> Any:
        """Run the command: validated input, only QGIS work here.

        ``params`` is the instance of this command's ``params_model``, which is
        why its type is left to the command: a command accepts its own model
        only, so naming any shared type here would state a promise that no
        command keeps.  The result must be JSON serializable; the gateway owns
        the envelope.
        """

        raise NotImplementedError(f"{self.name} does not run synchronously")

    def arun(self, ctx: ActionContext, params: Any) -> Any:
        """Start the command asynchronously and report its operation.

        ``params`` is this command's ``params_model`` instance, as in ``run``.
        Build the QGIS job on this thread and hand it to ``ctx.operations``,
        which owns the record and the completion callback.  The returned
        snapshot -- not the command's own result -- is what leaves the plugin.
        """

        raise NotImplementedError(f"{self.name} does not run asynchronously")
