"""The three ``processing.*`` operations (PRD v2 §3).

All three run on the main thread: QGIS's processing framework works on the project
and the layers it holds, and a run may hand result layers to that project, so it
cannot move to a worker (PRD §2, D5).

There is no algorithm allow-list and no ``runnable`` field.  ``list_algorithms``
reports every registered algorithm, ``get_algorithm_info`` reads the native
definitions, and ``run_algorithm`` is the one entry point for all of them; a call
that cannot succeed ends as a ``FAILED`` record instead of the algorithm
disappearing from the discovery side (PRD §3.2.4).

The boundary this module owns is the JSON one.  ``parameters`` is a finite JSON
tree keyed by native parameter names; a non-string key, a NaN, an infinity, bytes
or a QGIS/Qt object is refused before QGIS sees it, and a result that has no
lossless JSON form is refused rather than ``str()``-ed.  A run may touch a file
only through a parameter whose native definition says it is a file input (checked
with ``resolve_path``) or a layer the current project already holds; every
destination is refused unless it names QGIS's in-memory provider, because the
settings of this build grant no data-file write access (PRD §3.2.2, §8.3).  ``None``
always means "not provided": it is handed on for the native check to judge, so an
absent or null destination ends as ``INVALID_INPUT`` from QGIS instead of a path
verdict this module has no basis for.

A database target that arrives in ordinary ``string`` parameters -- a host, a user,
a schema, a table name -- is not judged at all, and that is an accepted boundary,
not an oversight: the trust boundary of this plugin is the in-process, trusted
provider, and a value that names no local file gives the path policy nothing to
measure (PRD §8.3.4, §12).  A value that names network I/O or a virtual file system
is different, because QGIS resolves it itself: a provided string of that kind is
refused before the algorithm is called (PRD §8.3.2).  Which parameter types that
refusal applies to is fixed by measurement rather than by a list of names that grows
one type at a time: ``_MEASURED_PARAMETER_TYPES`` is every type string the 440
algorithms registered in QGIS 3.44.13 report (53 of them), and ``_VALUE_PARAMETERS``
is that surface minus the types that name I/O.  The strict set is exactly the
destinations, ``file``, the seven layer-reference types (``source``, ``vector``,
``raster``, ``mesh``, ``layer``, ``multilayer``, ``pointcloud``), the three
provider-connection types and the five composite ones -- plus every type not among
the measured 53, because a type this build has never seen is not known to carry a
value.  A value type is not judged at all, so ``EPSG:32633`` in a ``crs``, a layout
name, a theme name, a colour or a plain string reaches QGIS as it arrived: it is data
QGIS parses, and refusing it would take away the standard spelling of more than fifty
algorithms' ``crs`` parameters (PRD §3.2.2).  What an unknown type keeps is the remote
refusal and nothing else -- a local path in an unknown type is still handed to QGIS
untouched, because only ``file`` and the layer-reference types are paths this module
has a rule for.

QGIS is imported inside the methods that use it, so this module loads in a bare
interpreter and its contracts stay testable without a running QGIS.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator

from ..core.contracts import (
    BaseCommand,
    BaseQuery,
    MainThreadOperationMixin,
    OperationContext,
)
from ..core.models import (
    INTERNAL_ERROR,
    INVALID_INPUT,
    LAYER_NOT_FOUND,
    PATH_NOT_ALLOWED,
    FrameworkError,
    Params,
)
from ..core.security import is_remote_uri, resolve_path

_LOGGER = logging.getLogger(__name__)

# The one destination value this build accepts: a layer that lives in memory only.
# ``QgsProcessing.TEMPORARY_OUTPUT`` is deliberately not in this namespace -- it can
# name a file on disk (PRD §3.2.2).
_MEMORY_OUTPUT = "memory:"

# QGIS parameter type strings, as the definition classes report them.  They are the
# public spelling shared by the model serialiser and ``qgis_process``, which is why
# this module can tell a file input from a layer reference without importing the
# definition classes.  A parameter type not named here is passed through untouched;
# the families below are the ones the PRD gives a boundary rule.
_FILE_PARAMETER = "file"
_LAYER_PARAMETERS = frozenset(
    {"source", "vector", "raster", "mesh", "layer", "multilayer", "pointcloud"}
)
_CONNECTION_PARAMETERS = frozenset({"providerconnection", "databaseschema", "databasetable"})

# These types carry layer ids *inside* their values, each in its own shape (measured on
# 3.44.13: ``native:alignrasters``, ``native:dxfexport``, ``native:tinmeshcreation``,
# ``native:writevectortiles_*`` and the old ``qgis:*interpolation`` family).  QGIS
# resolves those inner strings itself, so passing one through would load a source the
# path policy never saw (PRD §3.2.2, §8.3).  The plugin refuses the family until it
# learns each shape; ``relief_colors``, ``aggregates`` and ``meshdatasetgroups`` carry
# values only and still pass through.
_COMPOSITE_LAYER_PARAMETERS = frozenset(
    {
        "idw_interpolation_data",
        "tininputlayers",
        "alignrasterlayers",
        "dxflayers",
        "vectortilewriterlayers",
    }
)

# A JSON image that could not be produced, kept apart from a legitimate ``None``.
_NO_JSON = object()

# The parameter type strings the 440 algorithms registered in 3.44.13 report, measured
# in one probe over their definitions.  The boundary below is derived from this list
# instead of growing one name at a time: a new QGIS type shows up here first, and
# until it does it is an unknown type and keeps the remote refusal.
_MEASURED_PARAMETER_TYPES = frozenset(
    {
        "aggregates",
        "alignrasterlayers",
        "area",
        "attribute",
        "authcfg",
        "band",
        "boolean",
        "color",
        "coordinateoperation",
        "crs",
        "databaseschema",
        "databasetable",
        "distance",
        "duration",
        "dxflayers",
        "enum",
        "execute_sql",
        "expression",
        "extent",
        "field",
        "fields_mapping",
        "file",
        "fileDestination",
        "folderDestination",
        "geometry",
        "idw_interpolation_data",
        "layer",
        "layout",
        "layoutitem",
        "maptheme",
        "matrix",
        "mesh",
        "meshdatasetgroups",
        "meshdatasettime",
        "multilayer",
        "number",
        "point",
        "pointCloudDestination",
        "pointcloud",
        "providerconnection",
        "raster",
        "rasterDestination",
        "raster_calc_expression",
        "relief_colors",
        "scale",
        "sink",
        "source",
        "string",
        "tininputlayers",
        "vector",
        "vectorDestination",
        "vectorTileDestination",
        "vectortilewriterlayers",
    }
)

# The measured types that name I/O: a destination, one file, a layer reference, a
# provider connection, or a value with layer references inside it.  The seven
# destination types never reach the check -- ``isDestination()`` refuses them before
# it -- and are listed so the two sets account for every measured type.
_IO_PARAMETER_TYPES = (
    frozenset(
        {
            "fileDestination",
            "folderDestination",
            "pointCloudDestination",
            "rasterDestination",
            "sink",
            "vectorDestination",
            "vectorTileDestination",
        }
    )
    | _LAYER_PARAMETERS
    | _CONNECTION_PARAMETERS
    | _COMPOSITE_LAYER_PARAMETERS
    | {_FILE_PARAMETER}
)

# The measured types that carry a value: QGIS parses them, it never resolves them as
# I/O, so the remote-value refusal must not look at them.  ``crs`` and
# ``coordinateoperation`` are the two whose authoritative spelling (``EPSG:32633``,
# ``urn:ogc:def:crs:EPSG::32633``) matches the URI scheme shape; 3.44.13 has 60 ``crs``
# and 2 ``coordinateoperation`` parameters, and QGIS accepts both.  A type outside the
# measured set is not in here, so it keeps the refusal.
_VALUE_PARAMETERS = _MEASURED_PARAMETER_TYPES - _IO_PARAMETER_TYPES


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


class AlgorithmParameter(BaseModel):
    """One native parameter definition, with nothing invented (PRD §3.1.2, §8.1)."""

    model_config = ConfigDict(extra="forbid")

    name: StrictStr
    type: StrictStr
    description: StrictStr
    optional: StrictBool
    # ``default`` is the JSON image of ``defaultValue()``; ``null`` means "no default
    # QGIS could state", because a QGIS/Qt object or a non-finite number has none.
    default: Any
    help: StrictStr
    is_destination: StrictBool
    depends_on: list[StrictStr]
    # Only an enum parameter has display options; the base definition has no
    # ``options()`` at all, so ``null`` states that instead of a fabricated empty list.
    options: list[StrictStr] | None

    @field_validator("default")
    @classmethod
    def _default_is_json(cls, value: Any) -> Any:
        return _require_json(value)


class AlgorithmOutput(BaseModel):
    """One declared output; not a value a run produced (PRD §3.1.2)."""

    model_config = ConfigDict(extra="forbid")

    name: StrictStr
    type: StrictStr
    description: StrictStr


class ProcessingListAlgorithmsParams(Params):
    """Empty on purpose: the current registry is the only input (PRD §3.1.1)."""


class ProcessingListAlgorithmsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithms: list[AlgorithmSummary]


class ProcessingGetAlgorithmInfoParams(Params):
    algorithm_id: StrictStr = Field(min_length=1)


class ProcessingGetAlgorithmInfoResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithm: AlgorithmSummary
    description: StrictStr
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
    """The id that actually ran and every output under its native key (PRD §3.2.1)."""

    model_config = ConfigDict(extra="forbid")

    algorithm_id: StrictStr
    outputs: dict[str, Any]

    @field_validator("outputs")
    @classmethod
    def _outputs_are_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _require_json(value)


def algorithm_summary(algorithm: Any) -> AlgorithmSummary:
    """The shared id/name/group summary, so list and info cannot disagree."""

    return AlgorithmSummary(
        id=algorithm.id(),
        name=algorithm.displayName(),
        group=algorithm.group(),
    )


def parameter_summary(definition: Any) -> AlgorithmParameter:
    """One definition as the nine PRD fields, in its native order and wording."""

    image = _json_image(definition.defaultValue())
    return AlgorithmParameter(
        name=definition.name(),
        type=definition.type(),
        description=definition.description() or "",
        optional=_is_optional(definition),
        default=None if image is _NO_JSON else image,
        help=definition.help() or "",
        is_destination=bool(definition.isDestination()),
        depends_on=list(definition.dependsOnOtherParameters()),
        options=list(definition.options()) if hasattr(definition, "options") else None,
    )


def _is_optional(definition: Any) -> bool:
    """``FlagOptional`` is the definition's own statement, not a value judgement."""

    from qgis.core import QgsProcessingParameterDefinition

    return bool(definition.flags() & QgsProcessingParameterDefinition.FlagOptional)


def _resolve_parameter(definition: Any, value: Any, ctx: OperationContext) -> Any:
    """The value to hand QGIS for one declared parameter.

    ``None`` is "not provided" for every family, so the native check decides whether
    it is acceptable; every provided value is judged here.  A provided string that
    names network I/O or a virtual file system is refused unless the parameter's type
    is one of the measured types that carries a value (``_VALUE_PARAMETERS``) -- a
    type this build has never seen is not in that set, so it keeps the refusal --
    because QGIS resolves such a value itself and would do it before the path policy
    saw it; destinations, file inputs, layer references and the types that hide layer
    references then get a boundary rule of their own, while anything else is passed
    through as it arrived, because the plugin does not re-implement a second parameter
    schema (PRD §3.2.2, §8.3.2, §12).
    """

    if value is None:
        return value

    kind = definition.type()
    if definition.isDestination():
        if isinstance(value, str) and value.startswith(_MEMORY_OUTPUT):
            return value
        raise FrameworkError(PATH_NOT_ALLOWED, "only a memory output target is allowed")
    if kind not in _VALUE_PARAMETERS and isinstance(value, str) and is_remote_uri(value):
        # A URL, a UNC path or a ``/vsi`` path is I/O the path policy refuses outright,
        # and QGIS would resolve it before the policy ever saw it (PRD §8.3.2).  A type
        # that carries a value is exempt: its spelling is the algorithm's business, not
        # a path this module could judge.
        raise FrameworkError(PATH_NOT_ALLOWED, "a remote value is not an allowed input")

    if kind in _CONNECTION_PARAMETERS:
        raise FrameworkError(PATH_NOT_ALLOWED, "a provider connection is not an allowed input")
    if kind in _COMPOSITE_LAYER_PARAMETERS:
        raise FrameworkError(
            INVALID_INPUT, "a parameter with layer references inside its value is not supported"
        )
    if kind == _FILE_PARAMETER:
        if not isinstance(value, str):
            raise FrameworkError(INVALID_INPUT, "a file parameter takes one path")
        return str(resolve_path(value, ctx.policy.allowed_data_roots, must_exist=True))
    if kind in _LAYER_PARAMETERS:
        if isinstance(value, list):
            return [_resolve_layer_reference(item, ctx) for item in value]
        return _resolve_layer_reference(value, ctx)
    return value


def _resolve_layer_reference(value: Any, ctx: OperationContext) -> Any:
    """A project layer id stays an id; an allowed local file is the only other source.

    A string that names neither is refused: handing it to QGIS would let it load a
    provider URI the plugin never checked (PRD §3.2.2).
    """

    if value is None or value == "":
        return value
    if not isinstance(value, str):
        raise FrameworkError(INVALID_INPUT, "a layer parameter takes a layer id")
    if ctx.project.mapLayer(value) is not None:
        return value
    if is_remote_uri(value) or Path(value).is_absolute():
        # Either an existing file inside the roots or a refusal from the policy;
        # a relative path has no meaning here and stays a missing layer id.
        return str(resolve_path(value, ctx.policy.allowed_data_roots, must_exist=True))
    raise FrameworkError(LAYER_NOT_FOUND, "the layer does not exist in the project")


def _can_execute(algorithm: Any) -> tuple[bool, str]:
    """QGIS 3.44 binds ``canExecute()`` as ``(bool, message)``; a plain bool also reads."""

    outcome = algorithm.canExecute()
    if isinstance(outcome, tuple):
        executable = bool(outcome[0]) if outcome else False
        message = outcome[1] if len(outcome) > 1 else ""
        return executable, "" if message is None else str(message)
    return bool(outcome), ""


def _take_result_layer(
    context: Any, project: Any, layer_id: str, transferred: dict[str, str]
) -> str:
    """The project's stable id for a result layer, taking it out of the context.

    A result that names a layer already in the project keeps its id; a result that
    names a layer in the context's temporary store is added to the project once and
    reported by the id the project gave it (PRD §3.2.3 step 8).
    """

    stable = transferred.get(layer_id)
    if stable is not None:
        return stable
    store = context.temporaryLayerStore()
    if store is None or store.mapLayer(layer_id) is None:
        return layer_id
    layer = context.takeResultLayer(layer_id)
    if layer is None:
        raise FrameworkError(INTERNAL_ERROR, "a result layer could not be taken from the run")
    if project.addMapLayer(layer) is None:
        raise FrameworkError(INTERNAL_ERROR, "a result layer was not added to the project")
    transferred[layer_id] = layer.id()
    return layer.id()


def _normalise_result(context: Any, project: Any, value: Any, transferred: dict[str, str]) -> Any:
    """One result value as JSON, with any result layer moved into the project.

    The walk is recursive because a provider may return a list or a mapping of
    layers, and ``transferred`` makes a repeated id resolve to the same project
    layer instead of a stale one.  A value with no JSON image fails the run rather
    than being described by ``str()`` (PRD §3.2.3, §8.2).
    """

    if isinstance(value, str):
        return _take_result_layer(context, project, value, transferred)
    if isinstance(value, (list, tuple)):
        return [_normalise_result(context, project, item, transferred) for item in value]
    if isinstance(value, dict):
        normalised: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise FrameworkError(INTERNAL_ERROR, "an algorithm result key is not a string")
            normalised[key] = _normalise_result(context, project, item, transferred)
        return normalised
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
        from qgis.core import QgsApplication

        algorithm = QgsApplication.processingRegistry().algorithmById(request.algorithm_id)
        if algorithm is None:
            # A valid call naming an unknown algorithm is a caller input mistake; it
            # is not an unknown plugin operation (PRD §8.2).
            raise FrameworkError(INVALID_INPUT, "the algorithm is not registered")

        return ProcessingGetAlgorithmInfoResult(
            algorithm=algorithm_summary(algorithm),
            description=algorithm.shortHelpString() or "",
            parameters=[
                parameter_summary(definition) for definition in algorithm.parameterDefinitions()
            ],
            outputs=[
                AlgorithmOutput(
                    name=output.name(),
                    type=output.type(),
                    description=output.description() or "",
                )
                for output in algorithm.outputDefinitions()
            ],
        )


class ProcessingRunAlgorithm(
    BaseCommand[ProcessingRunAlgorithmParams, ProcessingRunAlgorithmResult],
    MainThreadOperationMixin[ProcessingRunAlgorithmParams, ProcessingRunAlgorithmResult],
):
    """Run one registered algorithm through the checks of PRD §3.2.3, in order."""

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
        from qgis.core import QgsApplication, QgsProcessingContext, QgsProcessingFeedback

        class ErrorCollectingFeedback(QgsProcessingFeedback):
            """A run reports a failure through the feedback, not through ``ok`` alone."""

            def __init__(self) -> None:
                super().__init__()
                self.errors: list[str] = []

            def reportError(self, message: str, fatalError: bool = False) -> None:
                self.errors.append(message)
                super().reportError(message, fatalError)

        registry = QgsApplication.processingRegistry()
        if registry.algorithmById(request.algorithm_id) is None:
            raise FrameworkError(INVALID_INPUT, "the algorithm is not registered")
        # The registry's own object is a prototype shared by every caller; a run gets
        # its own instance (PRD §3.2.3 step 2).
        algorithm = registry.createAlgorithmById(request.algorithm_id)
        if algorithm is None:
            raise FrameworkError(INTERNAL_ERROR, "QGIS could not create the algorithm")

        executable, why = _can_execute(algorithm)
        if not executable:
            _LOGGER.warning("processing algorithm cannot execute: %s", why)
            raise FrameworkError(INVALID_INPUT, "the algorithm cannot execute in this environment")

        definitions = {
            definition.name(): definition for definition in algorithm.parameterDefinitions()
        }
        if not set(request.parameters) <= set(definitions):
            # ``checkParameterValues`` does not reject unknown keys, so the plugin
            # has to (PRD §3.2.3 step 4).
            raise FrameworkError(
                INVALID_INPUT, "a parameter name is not declared by this algorithm"
            )
        parameters = {
            name: _resolve_parameter(definitions[name], value, ctx)
            for name, value in request.parameters.items()
        }

        context = QgsProcessingContext()
        # Layer ids are resolved against the project the engine holds, never the
        # process-wide singleton (PRD §1.3, §3.2.3 step 5).
        context.setProject(ctx.project)
        feedback = ErrorCollectingFeedback()

        valid, message = algorithm.checkParameterValues(parameters, context)
        if not valid:
            # The native message may quote a path or a value, so it only reaches the
            # local log (PRD §3.2.3 step 6).
            _LOGGER.warning("processing algorithm refused its parameters: %s", message)
            raise FrameworkError(INVALID_INPUT, "the algorithm refused the given parameter values")

        results, completed = algorithm.run(parameters, context, feedback)
        if not completed:
            raise FrameworkError(INTERNAL_ERROR, "the algorithm did not complete")
        if feedback.errors:
            # The native text may quote a host or a path, so it only reaches the
            # local log; a run that produced nothing after reporting an error is a
            # failed run, whatever ``ok`` said (PRD §8.2, §3.2.3 step 7).
            _LOGGER.warning("processing algorithm reported errors: %s", feedback.errors)
            if not results:
                raise FrameworkError(
                    INTERNAL_ERROR, "the algorithm reported an error and returned no result"
                )
        if not isinstance(results, dict):
            raise FrameworkError(INTERNAL_ERROR, "the algorithm returned no result mapping")

        transferred: dict[str, str] = {}
        try:
            outputs = _normalise_result(context, ctx.project, results, transferred)
            return ProcessingRunAlgorithmResult(algorithm_id=request.algorithm_id, outputs=outputs)
        except Exception:  # noqa: BLE001 - the layers this call added must go again
            # Only what this run added is undone: the algorithm's own external side
            # effects are not claimed to be rolled back (PRD §3.2.3).
            for layer_id in transferred.values():
                ctx.project.removeMapLayer(layer_id)
            raise


# The module's one tuple: ``operations/__init__.py`` imports it and must not define
# a second copy.
PROCESSING_OPERATIONS = (
    ProcessingListAlgorithms,
    ProcessingGetAlgorithmInfo,
    ProcessingRunAlgorithm,
)
