"""The operations this build ships.

The plugin hands its registry to ``register_all`` once, so adding an operation
edits the module that declares it and this list, and nothing in the entry point.
"""

from __future__ import annotations

from ..core.registry import OperationRegistry

__all__ = ["register_all"]


def register_all(registry: OperationRegistry) -> None:
    """Register every operation module of this build; empty until they land."""
