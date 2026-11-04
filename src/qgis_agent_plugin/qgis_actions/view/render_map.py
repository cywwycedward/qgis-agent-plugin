"""Render the current map view to an image file."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, Field, StrictInt

from ..base import Action, ActionContext, ActionError, BaseParams
from ..utils import absolute_path


class RenderMapParams(BaseParams):
    """Where to write the image and how many pixels to render.

    A path here is a file this command creates, so it is checked first: it has to
    be absolute, because QGIS would resolve a relative one against its own working
    directory rather than the caller's.
    """

    path: Annotated[str, Field(min_length=1, max_length=4096), AfterValidator(absolute_path)]
    width: StrictInt = Field(default=800, ge=1)
    height: StrictInt = Field(default=600, ge=1)


class RenderMap(Action):
    """Render the canvas extent to ``path`` at the requested pixel size."""

    name = "render_map"
    read_only = False
    execution = "both"
    params_model = RenderMapParams

    def _settings(self, ctx: ActionContext, params: RenderMapParams) -> Any:
        """Build the render settings; the canvas and the project are read here, on the main thread."""

        from qgis.core import QgsMapSettings
        from qgis.PyQt.QtCore import QSize

        canvas = ctx.iface.mapCanvas()
        # Start from the canvas settings so CRS, visible layers, rotation and
        # other view state cannot drift from what the user sees.
        settings = QgsMapSettings(canvas.mapSettings())
        settings.setExtent(canvas.extent())
        settings.setOutputSize(QSize(params.width, params.height))
        settings.setOutputDpi(96)
        return settings

    def run(self, ctx: ActionContext, params: RenderMapParams) -> dict[str, Any]:
        try:
            from qgis.core import QgsMapRendererParallelJob

            job = QgsMapRendererParallelJob(self._settings(ctx, params))
            job.start()
            job.waitForFinished()

            if not job.renderedImage().save(params.path):
                raise ActionError(
                    "render_failed",
                    f"Render error: Failed to save rendered image to {params.path}",
                )
        except ActionError:
            raise
        except Exception as error:
            raise ActionError("render_failed", f"Render error: {error}") from error
        return _rendered(params)

    def arun(self, ctx: ActionContext, params: RenderMapParams) -> dict[str, Any]:
        """Start the render and answer with its operation instead of waiting for it."""

        from qgis.core import QgsMapRendererParallelJob

        from ..jobs import RenderMapJob, start_operation

        job = RenderMapJob(
            QgsMapRendererParallelJob(self._settings(ctx, params)),
            params.path,
            lambda: _rendered(params),
        )
        return start_operation(ctx, self.name, job)


def _rendered(params: RenderMapParams) -> dict[str, Any]:
    """The answer one written render leaves as, in both execution modes.

    The background job is handed this answer to settle with, so the two paths
    cannot drift into two shapes.
    """

    return {
        "rendered": True,
        "path": params.path,
        "width": params.width,
        "height": params.height,
    }
