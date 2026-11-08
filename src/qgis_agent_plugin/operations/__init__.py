"""The operations this build ships (PRD 7.1).

Each module declares its own operations and lists them here as one tuple, so adding
an operation edits the module that declares it and its tuple, and nothing in the
entry point; the plugin hands its registry to ``register_all`` once and never names
an operation itself.  A duplicate name or a broken declaration is the registry's
refusal (PRD 6.4), deliberately not swallowed here: a build whose surface is wrong
must fail when it loads, not when someone calls it.
"""

from __future__ import annotations

from ..core.registry import OperationRegistry
from .layer import (
    LayerAdd,
    LayerGetInfo,
    LayerGetQml,
    LayerGetRenderer,
    LayerList,
    LayerListAvailableRenderers,
    LayerListStyleCategories,
    LayerSetRenderer,
    LayerSetVisibility,
    LayerSumNumericField,
)
from .processing import PROCESSING_OPERATIONS
from .project import ProjectCreate, ProjectGetInfo, ProjectOpen, ProjectSave

__all__ = ["LAYER_OPERATIONS", "OPERATIONS", "PROJECT_OPERATIONS", "register_all"]

PROJECT_OPERATIONS = (ProjectCreate, ProjectOpen, ProjectSave, ProjectGetInfo)
LAYER_OPERATIONS = (
    LayerAdd,
    LayerList,
    LayerGetInfo,
    LayerSetVisibility,
    LayerSumNumericField,
    LayerListStyleCategories,
    LayerGetQml,
    LayerListAvailableRenderers,
    LayerGetRenderer,
    LayerSetRenderer,
)

# In the order of the PRD v2 §2/§9 table: the four ``project.*``, then the ten
# ``layer.*``, then the three ``processing.*`` (whose tuple lives in ``processing``).
OPERATIONS = (*PROJECT_OPERATIONS, *LAYER_OPERATIONS, *PROCESSING_OPERATIONS)


def register_all(registry: OperationRegistry) -> None:
    """Register every operation of this build, in the order the PRD lists them."""

    for operation in OPERATIONS:
        registry.register(operation())
