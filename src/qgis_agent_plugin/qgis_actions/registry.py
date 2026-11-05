"""The wire command surface: one entry per command, in protocol order."""

from __future__ import annotations

from .base import Action
from .layer.add_layer import AddLayer
from .layer.get_layers import GetLayers
from .layer.remove_layer import RemoveLayer
from .layer.zoom_to_layer import ZoomToLayer
from .operations.cancel_operation import CancelOperation
from .operations.get_operation import GetOperation
from .processing.describe_processing_algorithm import DescribeProcessingAlgorithm
from .processing.execute_processing import ExecuteProcessing
from .processing.list_processing_algorithms import ListProcessingAlgorithms
from .project.create_new_project import CreateNewProject
from .project.get_project_info import GetProjectInfo
from .project.load_project import LoadProject
from .project.save_project import SaveProject
from .qgis.get_qgis_info import GetQGISInfo
from .qgis.ping import Ping
from .view.render_map import RenderMap

# The order the commands are declared in is the order the surface reports them.
ACTIONS: tuple[Action, ...] = (
    Ping(),
    GetQGISInfo(),
    LoadProject(),
    GetProjectInfo(),
    AddLayer(),
    GetLayers(),
    RemoveLayer(),
    ZoomToLayer(),
    ListProcessingAlgorithms(),
    ExecuteProcessing(),
    DescribeProcessingAlgorithm(),
    SaveProject(),
    RenderMap(),
    CreateNewProject(),
    GetOperation(),
    CancelOperation(),
)

REGISTRY: dict[str, Action] = {action.name: action for action in ACTIONS}
if len(REGISTRY) != len(ACTIONS):  # duplicate names must fail while importing
    raise RuntimeError("duplicate action name in ACTIONS")

COMMAND_NAMES: tuple[str, ...] = tuple(REGISTRY)
