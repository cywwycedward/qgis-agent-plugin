"""本构建随包提供的操作。

每个模块声明自己的操作并把它们列成一个元组，所以新增一个操作只改声明它的模块和那个元组，
入口一行都不用动；插件把注册表交给 ``register_all`` 一次，自己从不点名任何操作。
重名或写坏的声明由注册表拒绝，这里故意不吞：表面本身就错的构建必须在加载时失败，
而不是等人调用时才失败。

The operations this build ships.

Each module declares its own operations and lists them here as one tuple, so adding
an operation edits the module that declares it and its tuple, and nothing in the
entry point; the plugin hands its registry to ``register_all`` once and never names
an operation itself.  A duplicate name or a broken declaration is the registry's
refusal, deliberately not swallowed here: a build whose surface is wrong must fail
when it loads, not when someone calls it.
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

# 顺序就是对外表里的顺序：四个 ``project.*``、十个 ``layer.*``、再三个 ``processing.*``，
# 其中 processing 的元组在它自己的模块里。
# In the order of the published surface: the four ``project.*``, then the ten ``layer.*``,
# then the three ``processing.*``, whose tuple lives in ``processing``.
OPERATIONS = (*PROJECT_OPERATIONS, *LAYER_OPERATIONS, *PROCESSING_OPERATIONS)


def register_all(registry: OperationRegistry) -> None:
    """按对外表的顺序注册本构建的每个操作。

    Register every operation of this build, in the order the surface lists them.
    """

    for operation in OPERATIONS:
        registry.register(operation())
