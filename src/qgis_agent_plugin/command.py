"""What a command declares, and what one is handed when it runs.

A command class declares its wire ``name``, its ``params_model`` and its
``execution`` mode once, then implements ``run``, ``arun`` or both.  Everything
identical for every command -- envelope, parameter validation, the error
envelope, dumping the answer -- belongs to the pipeline, so the pipeline never
needs to know a specific command.

This module imports pydantic and nothing else: every declaration and its rules
stay usable in a plain interpreter, which is what lets the surface be listed
without QGIS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

# The wire modes each execution offers, default-first: an omitted request takes
# the first one, so a "both" command stays synchronous unless it is asked
# otherwise.  The pipeline reads the modes off the declaration, so this is the
# one place the vocabulary lives.
MODES = {"sync": ("sync",), "async": ("async",), "both": ("sync", "async")}
# The method each wire mode calls; a declaration has to implement the ones it offers.
_METHODS = {"sync": "run", "async": "arun"}
_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


@dataclass(frozen=True)
class CommandContext:
    """The runtime handles one command may use; commands never reach globals."""

    iface: Any = None
    project: Any = None
    operations: Any = None  # the Operations store, for asynchronous commands


class Params(BaseModel):
    """Base for one command's parameters: an unknown key is a caller mistake.

    Every command's model inherits this, so the rule cannot drift between
    commands, and a command with no parameters of its own gets an empty model
    that rejects anything.  A model holds params only: how a failure of one rule
    reads is decided where that rule is written, and the pipeline renders the
    collected failures once for every command.
    """

    model_config = ConfigDict(extra="forbid")


class Result(BaseModel):
    """Base for one command's result, in either execution mode.

    A command builds an instance and returns it; the pipeline dumps it exactly
    once, so a result may hold any value pydantic can serialize and a command
    never has to know how JSON spells it.  Inheriting the rule from one place
    also means a key the model does not declare is a mistake for what a command
    builds, not only for what it is called with.
    """

    model_config = ConfigDict(extra="forbid")


class Command:
    """One wire command, declared and implemented in one place."""

    name: ClassVar[str]
    params_model: ClassVar[type[Params]] = Params
    execution: ClassVar[str] = "sync"  # sync | async | both
    modes: ClassVar[tuple[str, ...]] = ("sync",)  # the wire modes execution offers

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Reject a malformed declaration while the surface is imported."""

        super().__init_subclass__(**kwargs)
        name = getattr(cls, "name", None)
        if not isinstance(name, str) or not _NAME.match(name):
            raise TypeError(f"invalid command name: {name!r}")
        if cls.execution not in MODES:
            raise TypeError(f"{name}: unsupported execution {cls.execution!r}")
        cls.modes = MODES[cls.execution]
        for method in (_METHODS[mode] for mode in cls.modes):
            if getattr(cls, method, None) is getattr(Command, method, None):
                raise TypeError(f"{name}: execution {cls.execution!r} requires {method}()")

    def run(self, ctx: CommandContext, params: Any) -> Result:
        """Run the command in this thread and return its result.

        ``params`` is the instance of this command's ``params_model``, which is
        why its type is left to the command: a command accepts its own model
        only, so naming a shared type here would state a promise no command
        keeps.
        """

        raise NotImplementedError(f"{self.name} does not run synchronously")

    def arun(self, ctx: CommandContext, params: Any) -> Result:
        """Start the command elsewhere and return its operation.

        ``params`` is this command's ``params_model`` instance, as in ``run``.
        Build the QGIS job on this thread and hand it to ``ctx.operations``,
        which owns the record and the completion callback.  The operation -- not
        the command's own result -- is what leaves the plugin.
        """

        raise NotImplementedError(f"{self.name} does not run asynchronously")
