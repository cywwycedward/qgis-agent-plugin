"""Render the current map view to an image file."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, Field, StrictInt

from ...command import Command, CommandContext, Params, Result
from ...operations import Operation
from ...protocol import CommandError
from ..shared import absolute_path


class RenderMapParams(Params):
    """Where to write the image and how many pixels to render.

    A path here is a file this command creates, so it is checked first: it has to
    be absolute, because QGIS would resolve a relative one against its own working
    directory rather than the caller's.
    """

    path: Annotated[str, Field(min_length=1, max_length=4096), AfterValidator(absolute_path)]
    width: StrictInt = Field(default=800, ge=1)
    height: StrictInt = Field(default=600, ge=1)


class RenderMapResult(Result):
    """The image one finished render wrote, in both execution modes.

    The background job is handed this answer to settle with, so the two paths
    cannot drift into two shapes.
    """

    rendered: bool
    path: str
    width: int
    height: int


class RenderMap(Command):
    """Render the canvas extent to ``path`` at the requested pixel size."""

    name = "render_map"
    execution = "both"
    params_model = RenderMapParams

    def _settings(self, ctx: CommandContext, params: RenderMapParams) -> Any:
        """Build the render settings, reading the canvas and the project on this thread."""

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

    def run(self, ctx: CommandContext, params: RenderMapParams) -> RenderMapResult:
        try:
            from qgis.core import QgsMapRendererParallelJob

            job = QgsMapRendererParallelJob(self._settings(ctx, params))
            job.start()
            job.waitForFinished()

            if not job.renderedImage().save(params.path):
                raise CommandError(
                    "render_failed",
                    f"Render error: Failed to save rendered image to {params.path}",
                )
        except CommandError:
            raise
        except Exception as error:
            raise CommandError("render_failed", f"Render error: {error}") from error
        return _rendered(params)

    def arun(self, ctx: CommandContext, params: RenderMapParams) -> Operation:
        """Start the render and answer with its operation instead of waiting for it."""

        from qgis.core import QgsMapRendererParallelJob

        from ...jobs import RenderMapJob

        job = RenderMapJob(
            QgsMapRendererParallelJob(self._settings(ctx, params)),
            params.path,
            lambda: _rendered(params),
        )
        return ctx.operations.start(self.name, job)


def _rendered(params: RenderMapParams) -> RenderMapResult:
    """The answer one written render leaves as, in both execution modes."""

    return RenderMapResult(
        rendered=True,
        path=params.path,
        width=params.width,
        height=params.height,
    )
