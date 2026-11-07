"""The four ``project.*`` operations (PRD 7.2-7.5).

All four run on the main thread, because a ``QgsProject`` may not be touched from
a worker (PRD 8.3), and the three that replace or write state declare themselves
destructive from the PRD 7.1 table, so a caller can see the risk without reading
the code.

``create`` and ``open`` share one guard: a project that holds anything worth
keeping -- layers, an associated file, unsaved changes -- is only replaced when
the caller says ``discard_current=true`` (PRD 7.2, 7.3, 10.3).  ``save`` has the
matching guard for files: an existing target is only replaced with
``overwrite=true`` (PRD 7.4, 10.3, AT-17).

QGIS is imported inside the one method that needs it, so this module still loads
in a bare interpreter and the path/CRS rules stay testable without QGIS.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, StrictBool, StrictStr

from ..core.contracts import (
    BaseCommand,
    BaseQuery,
    MainThreadOperationMixin,
    OperationContext,
)
from ..core.models import (
    INTERNAL_ERROR,
    INVALID_CRS,
    PROJECT_OPEN_FAILED,
    PROJECT_PATH_REQUIRED,
    PROJECT_REPLACE_REQUIRED,
    PROJECT_SAVE_FAILED,
    TARGET_EXISTS,
    FrameworkError,
    Params,
)
from ..core.security import resolve_path

# The two formats this plugin reads and writes; anything else is a caller mistake.
PROJECT_SUFFIXES = (".qgs", ".qgz")


def _project_path(raw: str) -> str:
    """Refuse a path whose shape is wrong before any file system call.

    Only the shape belongs here: whether the directory is allowed, and whether
    the file exists, is the path policy's decision (PRD 10.2), so a well-formed
    path still has to survive ``resolve_path``.
    """

    path = Path(raw)
    if not path.is_absolute() or path.suffix.lower() not in PROJECT_SUFFIXES:
        raise ValueError("path must be an absolute .qgs or .qgz path")
    return raw


class ProjectCreateParams(Params):
    crs_authid: StrictStr = Field(default="EPSG:4326", min_length=1)
    discard_current: StrictBool = False


class ProjectCreateResult(BaseModel):
    crs_authid: StrictStr
    file_path: StrictStr | None = None
    layer_count: int = 0


class ProjectOpenParams(Params):
    path: Annotated[StrictStr, AfterValidator(_project_path)]
    discard_current: StrictBool = False


class ProjectOpenResult(BaseModel):
    file_path: StrictStr
    crs_authid: StrictStr
    layer_count: int
    # AT-18B: a project whose layers cannot load is read, but it is not reported
    # as fully usable, and it is not silently called a failure either.
    invalid_layer_count: int


class ProjectSaveParams(Params):
    path: Annotated[StrictStr, AfterValidator(_project_path)] | None = None
    overwrite: StrictBool = False


class ProjectSaveResult(BaseModel):
    file_path: StrictStr
    is_dirty: StrictBool


class ProjectGetInfoParams(Params):
    pass


class ProjectGetInfoResult(BaseModel):
    file_path: StrictStr | None
    crs_authid: StrictStr
    is_dirty: StrictBool
    layer_count: int
    project_epoch: int


class ProjectCreate(
    BaseCommand[ProjectCreateParams, ProjectCreateResult],
    MainThreadOperationMixin[ProjectCreateParams, ProjectCreateResult],
):
    name = "project.create"
    description = "Replace the current project with a new empty one on a given CRS"
    input_model = ProjectCreateParams
    output_model = ProjectCreateResult
    destructive = True

    def execute_main(
        self, ctx: OperationContext, request: ProjectCreateParams
    ) -> ProjectCreateResult:
        from qgis.core import QgsCoordinateReferenceSystem

        # An unusable CRS is a caller mistake, and it is refused before the
        # current project is touched (PRD 7.2).
        crs = QgsCoordinateReferenceSystem(request.crs_authid)
        if not crs.isValid():
            raise FrameworkError(INVALID_CRS, f"{request.crs_authid} is not a usable CRS")
        _refuse_replacement_unless_discarding(ctx, request.discard_current)

        if not ctx.iface.newProject(promptToSaveFlag=False):
            # PRD 7.2 names no code for this: QGIS declining to create a project
            # is not a caller mistake and not a layer or path problem.
            raise FrameworkError(INTERNAL_ERROR, "QGIS did not create a new project")

        ctx.project.setCrs(crs)
        return ProjectCreateResult(
            # The caller's string may be a valid alias (``epsg:4326``); report the
            # canonical authid the project now holds, as ``open``/``get_info`` do.
            crs_authid=crs.authid(),
            file_path=ctx.project.fileName() or None,
            layer_count=len(ctx.project.mapLayers()),
        )


class ProjectOpen(
    BaseCommand[ProjectOpenParams, ProjectOpenResult],
    MainThreadOperationMixin[ProjectOpenParams, ProjectOpenResult],
):
    name = "project.open"
    description = "Replace the current project with an existing .qgs or .qgz file"
    input_model = ProjectOpenParams
    output_model = ProjectOpenResult
    destructive = True

    def execute_main(self, ctx: OperationContext, request: ProjectOpenParams) -> ProjectOpenResult:
        path = resolve_path(request.path, ctx.policy.allowed_project_roots, must_exist=True)
        _refuse_replacement_unless_discarding(ctx, request.discard_current)

        # `read()` rather than `iface.addProject()`, which may prompt: a refusal
        # has to be an error code, never a modal dialog (PRD 7.3).
        if not ctx.project.read(str(path)):
            raise FrameworkError(
                PROJECT_OPEN_FAILED,
                "the project file could not be read; the current project may have changed",
            )

        layers = ctx.project.mapLayers()
        return ProjectOpenResult(
            file_path=str(path),
            crs_authid=ctx.project.crs().authid(),
            layer_count=len(layers),
            invalid_layer_count=sum(1 for layer in layers.values() if not layer.isValid()),
        )


class ProjectSave(
    BaseCommand[ProjectSaveParams, ProjectSaveResult],
    MainThreadOperationMixin[ProjectSaveParams, ProjectSaveResult],
):
    name = "project.save"
    description = "Write the current project to a local .qgs or .qgz file"
    input_model = ProjectSaveParams
    output_model = ProjectSaveResult
    destructive = True

    def execute_main(self, ctx: OperationContext, request: ProjectSaveParams) -> ProjectSaveResult:
        project = ctx.project
        target = request.path or project.fileName()
        if not target:
            raise FrameworkError(
                PROJECT_PATH_REQUIRED, "no path was given and the project has no associated file"
            )

        path = resolve_path(target, ctx.policy.allowed_project_roots, must_exist=False)
        # The associated file counts too: overwriting it is a file loss decision
        # the caller has to make explicitly (PRD 7.4, AT-17).
        if path.exists() and not request.overwrite:
            raise FrameworkError(
                TARGET_EXISTS, "the target file exists; pass overwrite=true to replace it"
            )

        if not project.write(str(path)):
            raise FrameworkError(
                PROJECT_SAVE_FAILED,
                "the project could not be written; the target file may be partially written",
            )
        # `write()` clears the dirty flag on success, so this reports the project
        # as QGIS left it rather than echoing the PRD's sample output.
        return ProjectSaveResult(file_path=str(path), is_dirty=project.isDirty())


class ProjectGetInfo(
    BaseQuery[ProjectGetInfoParams, ProjectGetInfoResult],
    MainThreadOperationMixin[ProjectGetInfoParams, ProjectGetInfoResult],
):
    name = "project.get_info"
    description = "Read the current project's file, CRS, dirty flag, layer count and epoch"
    input_model = ProjectGetInfoParams
    output_model = ProjectGetInfoResult

    def execute_main(
        self, ctx: OperationContext, request: ProjectGetInfoParams
    ) -> ProjectGetInfoResult:
        project = ctx.project
        return ProjectGetInfoResult(
            file_path=project.fileName() or None,
            crs_authid=project.crs().authid(),
            is_dirty=project.isDirty(),
            layer_count=len(project.mapLayers()),
            project_epoch=ctx.lifecycle.current_epoch,
        )


def _refuse_replacement_unless_discarding(ctx: OperationContext, discard_current: bool) -> None:
    """Only an explicit ``discard_current=true`` may replace a project with content.

    PRD 7.2 also names an open edit session; a buffer is only open on a layer, so
    the layer count already covers it and there is no fourth check to look for.
    """

    if discard_current:
        return
    project = ctx.project
    if project.mapLayers() or project.fileName() or project.isDirty():
        raise FrameworkError(
            PROJECT_REPLACE_REQUIRED,
            "the current project holds layers, a file or unsaved changes; "
            "pass discard_current=true to replace it",
        )
