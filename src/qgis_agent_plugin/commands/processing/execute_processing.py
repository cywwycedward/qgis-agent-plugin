"""Run a QGIS processing algorithm and report what it produced.

The algorithm is QGIS's own, so the parameters are QGIS's own parameter map:
the names and the value shapes are whatever the algorithm declares.  What this
command adds is the step QGIS leaves to its window -- a layer output is brought
into the project, so the other commands can act on it, and reported the way
``get_layers`` reports a layer.

Both execution modes answer the same thing: the synchronous one runs the
algorithm on this thread and returns the answer, the background one hands it to
a ``QgsProcessingAlgRunnerTask`` and settles the same answer behind an
operation.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ...command import Command, CommandContext, Params, Result
from ...operations import Operation
from ...protocol import CommandError
from ..layer.report import layer_report
from .shared import require_algorithm


class ExecuteProcessingParams(Params):
    """A processing algorithm plus the parameters to run it with.

    ``parameters`` is handed to QGIS as it is, with one exception: a name the
    algorithm does not declare is refused.  QGIS ignores such a name in silence
    -- no warning, no error -- so a misspelled one would run the algorithm with
    a default value and answer as if the caller's value had been used.

    A destination parameter is the other thing to get right: QGIS gives it no
    default, so a run that leaves one out is refused.  ``TEMPORARY_OUTPUT`` asks
    for a temporary layer, and any other value is a path to write to.
    """

    algorithm: str = Field(min_length=1, max_length=256)
    parameters: dict[str, Any]


class ExecuteProcessingResult(Result):
    """What the algorithm answered, with its layer outputs in the project.

    ``result`` is keyed by the algorithm's own output names.  A layer output is
    reported like a layer from ``get_layers`` and is part of the project by the
    time this answer leaves, so ``get_layers``, ``zoom_to_layer`` and
    ``remove_layer`` can reach it; ``null`` is a layer output QGIS named and this
    command could not open.  A number or a boolean output keeps its JSON type,
    and everything else -- a file path, a string -- leaves as text.
    """

    algorithm: str = Field(description="Algorithm that ran, spelled as the caller asked for it.")
    result: dict[str, Any] = Field(description="One entry per output the algorithm reported.")


class ExecuteProcessing(Command):
    """Run ``algorithm`` with the caller's ``parameters``."""

    name = "execute_processing"
    execution = "both"
    params_model = ExecuteProcessingParams

    def run(self, ctx: CommandContext, params: ExecuteProcessingParams) -> ExecuteProcessingResult:
        import processing
        from qgis.core import QgsProcessingFeedback

        algorithm = _checked(ctx.project, params)
        feedback = QgsProcessingFeedback()
        context = _context(ctx.project, feedback)
        try:
            # As a child algorithm the layers stay in the context: without this
            # QGIS hands them to whoever called ``run``, and only this path would
            # have them -- the background task always keeps them in the context.
            results = processing.run(
                algorithm,
                params.parameters,
                context=context,
                feedback=feedback,
                is_child_algorithm=True,
            )
        except Exception as error:  # QGIS reports a failed run as an exception
            raise CommandError("processing_failed", f"Processing error: {error}") from error
        return _answer(ctx.project, context, params.algorithm, algorithm, results)

    def arun(self, ctx: CommandContext, params: ExecuteProcessingParams) -> Operation:
        """Hand the algorithm to a background task and answer with its operation."""

        from ...jobs import ProcessingFeedback, ProcessingJob

        algorithm = _checked(ctx.project, params)
        feedback = ProcessingFeedback()
        context = _context(ctx.project, feedback)

        def answer(results: dict[str, Any]) -> ExecuteProcessingResult:
            """Shape what the finished task produced the way the synchronous path does."""

            return _answer(ctx.project, context, params.algorithm, algorithm, results)

        job = ProcessingJob(algorithm, params.parameters, context, feedback, answer)
        return ctx.operations.start(self.name, job)


def _context(project: Any, feedback: Any) -> Any:
    """Build the execution context the way QGIS's own console entry point does.

    That entry point fills in more than a project: the invalid-geometry policy,
    the processing encoding setting, an expression context, and the canvas' time
    range when the canvas has one.  Both execution modes build the same context
    here, so they differ in nothing but who waits for the answer.
    """

    from processing.tools import dataobjects

    context = dataobjects.createContext(feedback)
    context.setProject(project)
    return context


def _checked(project: Any, params: ExecuteProcessingParams) -> Any:
    """Return the algorithm to run, having refused what QGIS would refuse.

    A parameter name the algorithm does not declare is refused here: QGIS
    ignores such a name in silence -- no warning, no error -- so a misspelled
    one would run the algorithm with a default value and answer as if the
    caller's value had been used.  What QGIS itself would refuse is refused in
    its own words.

    Both checks run on a context of their own, thrown away afterwards.  The
    context that runs the algorithm is handed to a background task, and a task
    must be given a context nothing has resolved parameters with: QGIS keeps
    what the check left there alive past the run, so handing a context that has
    already resolved parameters to the task crashes the next one.
    """

    algorithm = require_algorithm(params.algorithm)
    declared = [definition.name() for definition in algorithm.parameterDefinitions()]
    unknown = [name for name in params.parameters if name not in declared]
    if unknown:
        raise CommandError(
            "unknown_parameter",
            f"Unknown parameter for {algorithm.id()}: {', '.join(unknown)}. "
            f"It takes: {', '.join(declared) or 'no parameters'}",
        )
    ok, message = algorithm.checkParameterValues(params.parameters, _context(project, None))
    if not ok:
        raise CommandError("invalid_parameters", f"Invalid parameters: {message}")
    return algorithm


def _answer(
    project: Any, context: Any, identifier: str, algorithm: Any, results: dict[str, Any]
) -> ExecuteProcessingResult:
    """Shape an algorithm's results into the answer, registering layer outputs."""

    declared = {definition.name(): definition for definition in algorithm.outputDefinitions()}
    return ExecuteProcessingResult(
        algorithm=identifier,
        result={
            name: _output(project, context, declared.get(name), value)
            for name, value in results.items()
        },
    )


def _output(project: Any, context: Any, definition: Any, value: Any) -> Any:
    """One output's value: a layer is registered and reported, the rest keeps its type."""

    from qgis.core import (
        QgsProcessingOutputBoolean,
        QgsProcessingOutputMapLayer,
        QgsProcessingOutputMultipleLayers,
        QgsProcessingOutputNumber,
        QgsProcessingOutputPointCloudLayer,
        QgsProcessingOutputRasterLayer,
        QgsProcessingOutputVectorLayer,
        QgsProcessingOutputVectorTileLayer,
    )

    layer_outputs = (
        QgsProcessingOutputVectorLayer,
        QgsProcessingOutputRasterLayer,
        QgsProcessingOutputPointCloudLayer,
        QgsProcessingOutputMapLayer,
        QgsProcessingOutputVectorTileLayer,
    )
    if isinstance(definition, QgsProcessingOutputMultipleLayers):
        return [_layer_output(project, context, reference) for reference in value]
    if isinstance(definition, layer_outputs):
        return _layer_output(project, context, value)
    if isinstance(definition, (QgsProcessingOutputNumber, QgsProcessingOutputBoolean)):
        return value
    return str(value)


def _layer_output(project: Any, context: Any, reference: Any) -> Any:
    """Bring the layer an output names into the project, and report it.

    A temporary output sits in the context until it is taken out of it, and an
    output written to a file has to be opened; QGIS keeps ownership of either
    one in the context's temporary store, so the layer is taken out of that
    store before the project is handed it.  An output the project already holds
    -- an algorithm that registered its own layer -- is left where it is.
    ``None`` is a layer output that named something this command could not turn
    into a layer.
    """

    from qgis.core import QgsProcessingUtils

    reference = str(reference)
    layer = project.mapLayer(reference)
    if layer is None:
        layer = context.takeResultLayer(reference)
    if layer is None:
        resolved = QgsProcessingUtils.mapLayerFromString(reference, context)
        layer = None if resolved is None else context.temporaryLayerStore().takeMapLayer(resolved)
    if layer is None:
        return None
    if project.mapLayer(layer.id()) is None:
        project.addMapLayer(layer)
    return layer_report(layer)
