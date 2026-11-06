"""The command surface: one entry per command, in protocol order.

The order here is the order the surface reports, so a listing reads the same way
every time.  A duplicate name fails while this module is imported: two commands
answering to one name make the wire ambiguous, and a caller has no way to say
which one it meant.
"""

from __future__ import annotations

from ..command import Command
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

_INSTANCES: tuple[Command, ...] = (
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

# The surface the pipeline looks a command up in, and the names in the order it
# reports them: the dict keeps its insertion order, so there is one list.
COMMANDS: dict[str, Command] = {command.name: command for command in _INSTANCES}
if len(COMMANDS) != len(_INSTANCES):
    raise RuntimeError("duplicate command name in the surface")
NAMES: tuple[str, ...] = tuple(COMMANDS)
