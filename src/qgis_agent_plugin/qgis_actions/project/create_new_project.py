"""Create an empty project and write it as a compressed ``.qgz`` file.

Creating a project replaces the one in memory, and there is no undo for that, so
the caller's ``on_unsaved_changes`` policy decides whether it may happen and the
path is checked while the current project is still there to keep.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from pydantic import AfterValidator, Field

from ..base import Action, ActionContext, ActionError, BaseParams, BaseResult
from ..utils import absolute_path, qgz_path
from .utils import UnsavedPolicy, unsaved_refusal


class CreateNewProjectParams(BaseParams):
    """The file to create, what to title it, and what may become of the current one.

    A path here is a file this command creates, so it is checked first: it has to
    be absolute and to end in ``.qgz``, the format that is written.
    """

    path: Annotated[
        str,
        Field(min_length=1, max_length=4096),
        AfterValidator(absolute_path),
        AfterValidator(qgz_path),
    ]
    title: str | None = Field(default=None, max_length=1024)
    on_unsaved_changes: UnsavedPolicy = "refuse"


class CreateNewProjectResult(BaseResult):
    """The project file that was created, and the CRS it was created with.

    The CRS is the one thing this command decides without being asked, so it is
    reported: the default QGIS uses for a new project, unless the application has
    been set to another.
    """

    path: str
    crs: str


class CreateNewProject(Action):
    """Replace the current project with an empty one and write it to ``path``."""

    name = "create_new_project"
    read_only = False
    params_model = CreateNewProjectParams

    def run(self, ctx: ActionContext, params: CreateNewProjectParams) -> dict[str, Any]:
        from qgis.core import Qgis, QgsCoordinateReferenceSystem, QgsSettings

        project = ctx.project
        # Anything the current project would lose stops the creation: its own
        # changes, which a save can still keep, and the layers no save can keep.
        refusal = unsaved_refusal(
            project, params.on_unsaved_changes, "a new empty project"
        )
        if refusal is not None:
            raise ActionError("unsaved_changes", refusal)

        # Clearing cannot be undone, so a path that cannot be written is refused
        # while the current project is still here to keep.
        directory = os.path.dirname(params.path)
        if not os.path.isdir(directory):
            raise ActionError(
                "save_failed",
                f"Cannot create {params.path}: {directory} is not a directory. "
                "The current project was left as it is.",
            )

        project.clear()
        # A written project is meant to travel with its data, and clear() keeps
        # whichever mode the project it replaced had, so this is set, not left.
        project.setFilePathStorage(Qgis.FilePathType.Relative)
        if params.title is not None:
            project.setTitle(params.title)
        # QGIS's own New project does this, from the same setting, so what this
        # creates is a project QGIS could have created by hand.
        project.setCrs(
            QgsCoordinateReferenceSystem(
                QgsSettings().value(
                    "/projections/defaultProjectCrs",
                    Qgis.geographicCrsAuthId(),
                    section=QgsSettings.App,
                )
            )
        )

        if not project.write(params.path):
            # write() keeps the path it was given even when it fails, and this
            # project has no file of its own: it is the empty one it became.
            project.setFileName("")
            raise ActionError(
                "save_failed",
                project.error() or f"Failed to create project {params.path}",
            )

        return CreateNewProjectResult(
            # QGIS's own spelling of the path, which is what later commands see.
            path=project.fileName(),
            crs=project.crs().authid(),
        ).model_dump(mode="json")
