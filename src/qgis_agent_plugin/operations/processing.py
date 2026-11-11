"""The three ``processing.*`` operations (PRD v2 §3).

All three run on the main thread: QGIS's processing framework works on the project
and the layers it holds, and a run hands result layers to that project, so it cannot
move to a worker (PRD §2, D5).

There is no algorithm allow-list and no ``runnable`` field: ``list_algorithms``
reports every registered algorithm, ``get_algorithm_info`` reads the native
definitions -- the fields ``algorithmHelp()`` prints, as JSON -- and
``run_algorithm`` is the one entry point for all of them.

``run_algorithm`` does not re-implement QGIS.  It hands the call to
``qgis.processing.run()`` with nothing but a feedback that keeps the run's messages,
so whether the algorithm exists, whether its parameter values are acceptable, how
the processing context is built and how the algorithm executes are all decided by
QGIS (design §5.2-§5.5).  An undeclared parameter key is QGIS's to ignore, a path or
a remote URI is QGIS's to resolve, and a parameter this module never classifies
reaches the algorithm exactly as it arrived.  A failure is an exception from
``qgis.processing.run()``: the feedback records and the exception go to the local
log and the exception is raised unchanged, so the engine reports it the one way it
reports an unexpected failure (design §5.6).  Nothing here classifies an exception
text into a plugin error code.

The boundary this module still owns is the JSON one.  ``parameters`` is a finite
JSON tree keyed by native parameter names; a non-string key, a NaN, an infinity,
bytes or a QGIS/Qt object is refused before QGIS sees it, and a result that has no
lossless JSON form is refused rather than ``str()``-ed.  A result layer is the one
value translated: ``qgis.processing.run()`` hands back real ``QgsMapLayer`` objects
that belong to no project, so each is added to the engine's project and reported by
its id; the layers this call added are removed again when its result cannot be
returned, and nothing claims the algorithm's own side effects were undone (design
§5.7).

QGIS is imported inside the methods that use it, so this module loads in a bare
interpreter and its contracts stay testable without a running QGIS.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator

from ..core.contracts import (
    BaseCommand,
    BaseQuery,
    MainThreadOperationMixin,
    OperationContext,
)
from ..core.models import INTERNAL_ERROR, INVALID_INPUT, FrameworkError, Params

_LOGGER = logging.getLogger(__name__)

# A JSON image that could not be produced, kept apart from a legitimate ``None``.
_NO_JSON = object()


def _json_image(value: Any) -> Any:
    """The JSON form of ``value``, or ``_NO_JSON`` when there is no lossless one.

    Only the exact JSON types pass.  ``bool``, ``int`` and ``float`` are compared by
    exact type on purpose: a Qt enum is an ``int`` subclass in some builds and an
    arbitrary number subclass is not JSON either.  Containers are copied into plain
    lists and dicts, so a tuple or a mapping subclass still comes out as JSON while
    an object of any other kind has no image at all.
    """

    kind = type(value)
    if value is None or kind is bool or kind is int or kind is str:
        return value
    if kind is float:
        return value if math.isfinite(value) else _NO_JSON
    if isinstance(value, (list, tuple)):
        items = [_json_image(item) for item in value]
        return _NO_JSON if any(item is _NO_JSON for item in items) else items
    if isinstance(value, dict):
        image: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                return _NO_JSON
            child = _json_image(item)
            if child is _NO_JSON:
                return _NO_JSON
            image[key] = child
        return image
    return _NO_JSON


def _require_json(value: Any) -> Any:
    """``value`` as a JSON tree, or a ``ValueError`` for the model to report."""

    image = _json_image(value)
    if image is _NO_JSON:
        raise ValueError("only finite JSON values are allowed")
    return image


class AlgorithmSummary(BaseModel):
    """One registered algorithm, in the terms the caller can act on (PRD §3.1.1)."""

    model_config = ConfigDict(extra="forbid")

    id: StrictStr
    name: StrictStr
    group: StrictStr


class AlgorithmDetails(BaseModel):
    """The algorithm itself, as its own definition states it (design §3.2, §3.5)."""

    model_config = ConfigDict(extra="forbid")

    id: StrictStr
    name: StrictStr
    short_description: StrictStr
    help: StrictStr


class AlgorithmParameter(BaseModel):
    """One visible native parameter definition (design §3.3, §3.5).

    ``options`` is only an enum's display list; the base definition has no
    ``options()`` at all, so ``None`` states that instead of a fabricated empty list.
    ``accepted_types`` is what the parameter type object says can be passed in, and
    an empty list when the registry has no type object for it.
    """

    model_config = ConfigDict(extra="forbid")

    name: StrictStr
    description: StrictStr
    help: StrictStr
    type: StrictStr
    options: list[StrictStr] | None
    accepted_types: list[StrictStr]


class AlgorithmOutput(BaseModel):
    """One declared output; not a value a run produced (design §3.4, §3.5)."""

    model_config = ConfigDict(extra="forbid")

    name: StrictStr
    type: StrictStr
    description: StrictStr


class ProcessingMessage(BaseModel):
    """One message a run kept, at the level of the feedback method that took it."""

    model_config = ConfigDict(extra="forbid")

    # info/debug/command/console/formatted/warning/error: the seven feedback methods.
    level: StrictStr
    text: StrictStr
    fatal: StrictBool = False


class ProcessingListAlgorithmsParams(Params):
    """Empty on purpose: the current registry is the only input (PRD §3.1.1)."""


class ProcessingListAlgorithmsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithms: list[AlgorithmSummary]


class ProcessingGetAlgorithmInfoParams(Params):
    algorithm_id: StrictStr = Field(min_length=1)


class ProcessingGetAlgorithmInfoResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithm: AlgorithmDetails
    parameters: list[AlgorithmParameter]
    outputs: list[AlgorithmOutput]


class ProcessingRunAlgorithmParams(Params):
    """A native algorithm id and its native parameter map (PRD §3.2.1)."""

    algorithm_id: StrictStr = Field(min_length=1)
    parameters: dict[str, Any]

    @field_validator("parameters")
    @classmethod
    def _parameters_are_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _require_json(value)


class ProcessingRunAlgorithmResult(BaseModel):
    """The id that ran, every output under its native key, and the run's messages."""

    model_config = ConfigDict(extra="forbid")

    algorithm_id: StrictStr
    outputs: dict[str, Any]
    messages: list[ProcessingMessage]

    @field_validator("outputs")
    @classmethod
    def _outputs_are_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _require_json(value)


def algorithm_summary(algorithm: Any) -> AlgorithmSummary:
    """The list's one summary shape: what the registry says about an algorithm."""

    return AlgorithmSummary(
        id=algorithm.id(),
        name=algorithm.displayName(),
        group=algorithm.group(),
    )


def _parameter_info(definition: Any, registry: Any) -> AlgorithmParameter:
    """One visible definition as the six design fields, in its native wording."""

    from qgis.core import QgsProcessingParameterEnum

    parameter_type = registry.parameterType(definition.type())
    return AlgorithmParameter(
        name=definition.name(),
        description=definition.description() or "",
        help=definition.help() or "",
        type=definition.__class__.__name__,
        options=list(definition.options())
        if isinstance(definition, QgsProcessingParameterEnum)
        else None,
        accepted_types=(
            [] if parameter_type is None else list(parameter_type.acceptedPythonTypes())
        ),
    )


def _json_result(project: Any, value: Any, added: set[str]) -> Any:
    """One result value as JSON, with every result layer added to ``project``.

    ``qgis.processing.run()`` hands the layers it produced over with their ownership
    and no project, so each one is added once and reported by its id; a layer this
    call already added resolves to the same id.  Anything else is JSON or a failure:
    a value with no lossless image is refused, never described by ``str()``.
    """

    from qgis.core import QgsMapLayer

    if isinstance(value, QgsMapLayer):
        layer_id = value.id()
        if layer_id not in added:
            if project.addMapLayer(value) is None:
                raise FrameworkError(INTERNAL_ERROR, "a result layer was not added to the project")
            added.add(layer_id)
        return layer_id
    if isinstance(value, (list, tuple)):
        return [_json_result(project, item, added) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise FrameworkError(INTERNAL_ERROR, "an algorithm result key is not a string")
            result[key] = _json_result(project, item, added)
        return result
    image = _json_image(value)
    if image is _NO_JSON:
        raise FrameworkError(INTERNAL_ERROR, "an algorithm result cannot be expressed as JSON")
    return image


class ProcessingListAlgorithms(
    BaseQuery[ProcessingListAlgorithmsParams, ProcessingListAlgorithmsResult],
    MainThreadOperationMixin[ProcessingListAlgorithmsParams, ProcessingListAlgorithmsResult],
):
    """Every registered algorithm, unfiltered and in id order (PRD §3.1.1)."""

    name = "processing.list_algorithms"
    description = "List every algorithm currently registered with QGIS Processing"
    input_model = ProcessingListAlgorithmsParams
    output_model = ProcessingListAlgorithmsResult

    def execute_main(
        self, ctx: OperationContext, request: ProcessingListAlgorithmsParams
    ) -> ProcessingListAlgorithmsResult:
        from qgis.core import QgsApplication

        algorithms = [
            algorithm_summary(algorithm)
            for algorithm in QgsApplication.processingRegistry().algorithms()
        ]
        algorithms.sort(key=lambda summary: summary.id)
        return ProcessingListAlgorithmsResult(algorithms=algorithms)


class ProcessingGetAlgorithmInfo(
    BaseQuery[ProcessingGetAlgorithmInfoParams, ProcessingGetAlgorithmInfoResult],
    MainThreadOperationMixin[ProcessingGetAlgorithmInfoParams, ProcessingGetAlgorithmInfoResult],
):
    """The native definition of one algorithm, in the order QGIS declares it."""

    name = "processing.get_algorithm_info"
    description = "Read one registered algorithm's native parameters and outputs"
    input_model = ProcessingGetAlgorithmInfoParams
    output_model = ProcessingGetAlgorithmInfoResult

    def execute_main(
        self, ctx: OperationContext, request: ProcessingGetAlgorithmInfoParams
    ) -> ProcessingGetAlgorithmInfoResult:
        from qgis.core import QgsApplication, QgsProcessingParameterDefinition

        registry = QgsApplication.processingRegistry()
        algorithm = registry.algorithmById(request.algorithm_id)
        if algorithm is None:
            # A valid call naming an unknown algorithm is a caller input mistake; it
            # is not an unknown plugin operation (design §3.5).
            raise FrameworkError(INVALID_INPUT, "the algorithm is not registered")

        # ``algorithmHelp()`` skips a hidden parameter, and so does this (design §3.3).
        hidden = QgsProcessingParameterDefinition.FlagHidden
        return ProcessingGetAlgorithmInfoResult(
            algorithm=AlgorithmDetails(
                id=algorithm.id(),
                name=algorithm.displayName(),
                short_description=algorithm.shortDescription() or "",
                help=algorithm.shortHelpString() or "",
            ),
            parameters=[
                _parameter_info(definition, registry)
                for definition in algorithm.parameterDefinitions()
                if not definition.flags() & hidden
            ],
            outputs=[
                AlgorithmOutput(
                    name=output.name(),
                    type=output.__class__.__name__,
                    description=output.description() or "",
                )
                for output in algorithm.outputDefinitions()
            ],
        )


class ProcessingRunAlgorithm(
    BaseCommand[ProcessingRunAlgorithmParams, ProcessingRunAlgorithmResult],
    MainThreadOperationMixin[ProcessingRunAlgorithmParams, ProcessingRunAlgorithmResult],
):
    """Run one registered algorithm through QGIS, and keep what it reported."""

    name = "processing.run_algorithm"
    description = "Run any algorithm currently registered with QGIS Processing"
    input_model = ProcessingRunAlgorithmParams
    output_model = ProcessingRunAlgorithmResult
    # The name stands for a group of algorithms that may change data or the project,
    # so the risk label cannot depend on the particular call (PRD §2).
    destructive = True

    def execute_main(
        self, ctx: OperationContext, request: ProcessingRunAlgorithmParams
    ) -> ProcessingRunAlgorithmResult:
        from qgis import processing
        from qgis.core import QgsProcessingFeedback

        class ProcessingRunFeedback(QgsProcessingFeedback):
            """The base class's own logging, plus one record per message (design §4)."""

            def __init__(self) -> None:
                super().__init__()
                self.records: list[ProcessingMessage] = []

            def _record(self, level: str, text: str, fatal: bool = False) -> None:
                self.records.append(ProcessingMessage(level=level, text=text, fatal=fatal))

            def pushInfo(self, info: str) -> None:
                super().pushInfo(info)
                self._record("info", info)

            def pushDebugInfo(self, info: str) -> None:
                super().pushDebugInfo(info)
                self._record("debug", info)

            def pushCommandInfo(self, info: str) -> None:
                super().pushCommandInfo(info)
                self._record("command", info)

            def pushConsoleInfo(self, info: str) -> None:
                super().pushConsoleInfo(info)
                self._record("console", info)

            def pushFormattedMessage(self, html: str, text: str) -> None:
                super().pushFormattedMessage(html, text)
                self._record("formatted", text)

            def pushWarning(self, warning: str) -> None:
                super().pushWarning(warning)
                self._record("warning", warning)

            def reportError(self, message: str, fatalError: bool = False) -> None:
                super().reportError(message, fatalError)
                self._record("error", message, bool(fatalError))

            @property
            def errors(self) -> list[str]:
                return [record.text for record in self.records if record.level == "error"]

            @property
            def has_errors(self) -> bool:
                return any(record.level == "error" for record in self.records)

        feedback = ProcessingRunFeedback()
        try:
            # Only the feedback: QGIS creates the context, checks the parameters and
            # runs the algorithm itself (design §5.2-§5.5).
            results = processing.run(request.algorithm_id, request.parameters, feedback=feedback)
        except Exception:
            # The native messages and the exception may quote a path or a host, so
            # they reach the local log and not the record; the caller gets the
            # engine's own unexpected-failure verdict (design §5.6).
            for record in feedback.records:
                _LOGGER.warning(
                    "processing %s reported %s: %s", request.algorithm_id, record.level, record.text
                )
            _LOGGER.exception("processing.run(%s) raised", request.algorithm_id)
            raise

        added: set[str] = set()
        try:
            return ProcessingRunAlgorithmResult(
                algorithm_id=request.algorithm_id,
                # ``None`` is QGIS's "no result", which is an empty one here.
                outputs=_json_result(ctx.project, {} if results is None else results, added),
                messages=feedback.records,
            )
        except Exception:  # noqa: BLE001 - the layers this call added must go again
            # Only what this call added is undone: the algorithm's own external side
            # effects are not claimed to be rolled back (design §5.7).
            for layer_id in added:
                ctx.project.removeMapLayer(layer_id)
            raise


# The module's one tuple: ``operations/__init__.py`` imports it and must not define
# a second copy.
PROCESSING_OPERATIONS = (
    ProcessingListAlgorithms,
    ProcessingGetAlgorithmInfo,
    ProcessingRunAlgorithm,
)
