"""Load a project file into the running QGIS application.

Reading a project replaces the one in memory, and ``QgsProject.read`` never looks
at ``isDirty``, so the caller's ``on_unsaved_changes`` policy is the only thing
standing between a load and an unsaved project.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, Field, StrictBool

from ..base import Action, ActionContext, ActionError, BaseParams, BaseResult
from ..utils import absolute_path
from .utils import UnsavedPolicy, unsaved_refusal

_SILENT_BAD_LAYER_HANDLER = None  # the most recent handler; reassigned every call


def _silent_bad_layer_handler():
    """A bad-layer handler that keeps unresolvable layers without asking.

    ``load_project`` is a headless operation: the caller learns which layers could
    not be resolved from ``broken_layer_count``.  QGIS Desktop's default handler
    instead opens the modal "Handle Unavailable Layers" dialog, which blocks the
    whole process (measured 2026-10-04: ``QgsProject.read`` hangs on it).  A no-op
    handler keeps the bad layers in the project so the count still reports them.
    """

    global _SILENT_BAD_LAYER_HANDLER
    from qgis.core import QgsProjectBadLayerHandler

    class _Silent(QgsProjectBadLayerHandler):
        def handleBadLayers(self, layers):
            pass

    # A fresh instance every call: QgsProject.setBadLayerHandler deletes the handler
    # it replaces, so a cached instance would be handed back after its C++ side was
    # gone.  The module global keeps the fresh one alive past setBadLayerHandler.
    _SILENT_BAD_LAYER_HANDLER = _Silent()
    return _SILENT_BAD_LAYER_HANDLER


class LoadProjectParams(BaseParams):
    """The project to read, and what may become of the one it replaces.

    A load never writes the current project: ``discard`` replaces it, while
    ``refuse`` and ``save`` fail on unsaved changes so the caller decides, and
    differ only in what the failure asks for.
    """

    path: Annotated[str, Field(min_length=1, max_length=4096), AfterValidator(absolute_path)]
    force_read_only_layers: StrictBool = False
    on_unsaved_changes: UnsavedPolicy = "refuse"


class LoadProjectResult(BaseResult):
    """The project QGIS holds once the load succeeded.

    ``broken_layer_count`` counts the layers QGIS kept in the project without
    resolving their data, which is also what opens QGIS's own
    handle-unavailable-layers dialog while the load is running.
    """

    path: str
    layer_count: int = Field(ge=0)
    broken_layer_count: int = Field(ge=0)


class LoadProject(Action):
    """Read ``path`` as the current project, per the caller's unsaved policy."""

    name = "load_project"
    params_model = LoadProjectParams

    def run(self, ctx: ActionContext, params: LoadProjectParams) -> dict[str, Any]:
        from qgis.core import Qgis

        project = ctx.project
        # Anything the current project would lose stops the load: its own changes,
        # which a save can still keep, and the layers no save can keep.
        refusal = unsaved_refusal(project, params.on_unsaved_changes, "this load")
        if refusal is not None:
            raise ActionError("unsaved_changes", refusal)

        flags = Qgis.ProjectReadFlags()
        if params.force_read_only_layers:
            flags |= Qgis.ProjectReadFlag.ForceReadOnlyLayers
        project.setBadLayerHandler(_silent_bad_layer_handler())
        if not project.read(params.path, flags):
            raise ActionError(
                "load_failed",
                project.error() or f"Failed to load project from {params.path}",
            )

        layers = project.mapLayers()
        return LoadProjectResult(
            # QGIS's own spelling of the path, which is what later commands see.
            path=project.fileName(),
            layer_count=len(layers),
            broken_layer_count=len(layers) - len(project.mapLayers(validOnly=True)),
        ).model_dump(mode="json")
