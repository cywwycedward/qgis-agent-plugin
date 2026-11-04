"""Private QGIS action layer: one class per wire command.

The gateway owns the wire envelope and the pipeline; this package owns the
commands themselves.  QGIS is imported inside ``run`` only, so every
declaration and its validation stays usable in a plain interpreter.
"""

from .base import Action, ActionContext, ActionError, BaseParams, BaseResult
from .registry import ACTIONS, COMMAND_NAMES, REGISTRY

__all__ = [
    "ACTIONS",
    "COMMAND_NAMES",
    "REGISTRY",
    "Action",
    "ActionContext",
    "ActionError",
    "BaseParams",
    "BaseResult",
]
