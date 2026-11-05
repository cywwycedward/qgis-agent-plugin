"""Save the current project as a compressed ``.qgz`` file.

The file holds the project, not the data: a layer's uncommitted edits and a
memory layer's features live only in this session and do not go into it.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, Field

from ..base import Action, ActionContext, ActionError, BaseParams, BaseResult
from ..utils import absolute_path, qgz_path


class SaveProjectParams(BaseParams):
    """Where to write the project; omitted means its own current path.

    A path given here is a file this command creates, so it is checked first: it
    has to be absolute and to end in ``.qgz``, the format that is written.
    """

    path: (
        Annotated[
            str,
            Field(min_length=1, max_length=4096),
            AfterValidator(absolute_path),
            AfterValidator(qgz_path),
        ]
        | None
    ) = None


class SaveProjectResult(BaseResult):
    """Where the project was written, and the file it came from.

    ``origin_path`` is empty when the project had no file yet.  These two paths
    are what tell a save from a save as: the same path twice is a save, while a
    different ``current_path`` means the project was written to a new file and
    ``origin_path`` was left as it was.
    """

    origin_path: str
    current_path: str


class SaveProject(Action):
    """Write the project to ``path``, or to its own path when omitted."""

    name = "save_project"
    read_only = False
    params_model = SaveProjectParams

    def run(self, ctx: ActionContext, params: SaveProjectParams) -> dict[str, Any]:
        from qgis.core import Qgis

        project = ctx.project
        # No path means the project's own file; a path means a new one, which the
        # write then adopts as the project's file.  Which of the two happened is
        # what the result reports, not something the caller has to know before.
        path = params.path or project.fileName()
        if not path:
            raise ActionError(
                "missing_path",
                "No project path given and the current project has none to save to; "
                "pass path as an absolute path ending in .qgz",
            )
        # A written project is meant to travel with its data, so paths are stored
        # relative to it; a layer no relative path can reach stays absolute by
        # itself, which is why nothing has to be checked for that here.
        origin_path = project.fileName()
        previous_dirty = project.isDirty()
        previous_storage = project.filePathStorage()
        project.setFilePathStorage(Qgis.FilePathType.Relative)

        if not project.write(path):
            # A failed write keeps the path it was given -- and, for a .qgz it
            # cannot zip, clears the dirty flag as well -- so both are put back:
            # the caller asked for the project to be saved, not to be relabelled
            # as saved at a path that does not exist.
            project.setFileName(origin_path)
            project.setFilePathStorage(previous_storage)
            project.setDirty(previous_dirty)
            raise ActionError("save_failed", project.error() or f"Failed to save project to {path}")

        return SaveProjectResult(
            origin_path=origin_path,
            # QGIS's own spelling of the path, which is what later commands see.
            current_path=project.fileName(),
        ).model_dump(mode="json")
